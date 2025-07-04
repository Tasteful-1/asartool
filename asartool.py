#Author : albsko
#Url : https://github.com/albsko/asarPy
#Modified : tasteful-1

import os
import errno
import io
import struct
import shutil
import json
from pathlib import Path
from typing import Union, Dict, Any


def round_up(i: int, m: int) -> int:
    return (i + m - 1) & ~(m - 1)


class Asar:
    def __init__(self, path: str, fp: io.IOBase, header: Dict[str, Any], base_offset: int):
        self.path = path
        self.fp = fp
        self.header = header
        self.base_offset = base_offset

    @classmethod
    def open(cls, path: Union[str, Path]):
        """Open an ASAR file"""
        path = str(path)
        try:
            fp = open(path, 'rb')
            data_size, header_size, header_object_size, header_string_size = struct.unpack('<4I', fp.read(16))
            header_json = fp.read(header_string_size).decode('utf-8')
            return cls(
                path=path,
                fp=fp,
                header=json.loads(header_json),
                base_offset=round_up(16 + header_string_size, 4)
            )
        except Exception as e:
            raise Exception(f"Failed to open ASAR file: {e}")

    @classmethod
    def compress(cls, path: Union[str, Path], exclude_patterns: list = None):
        """Compress a directory into ASAR format"""
        path = str(path)
        exclude_patterns = exclude_patterns or []
        offset = 0
        paths = []

        def should_exclude(file_path: str) -> bool:
            """Check if file should be excluded based on patterns"""
            for pattern in exclude_patterns:
                if pattern in file_path:
                    return True
            return False

        def _path_to_dict(dir_path: str) -> Dict[str, Any]:
            nonlocal offset, paths
            result = {'files': {}}

            try:
                for f in os.scandir(dir_path):
                    if should_exclude(f.path):
                        continue

                    if os.path.isdir(f.path):
                        result['files'][f.name] = _path_to_dict(f.path)
                    elif f.is_symlink():
                        result['files'][f.name] = {
                            'link': os.path.realpath(f.name)
                        }
                    else:
                        paths.append(f.path)
                        size = f.stat().st_size
                        result['files'][f.name] = {
                            'size': size,
                            'offset': str(offset)
                        }
                        offset += size
            except PermissionError as e:
                print(f"Skipping directory due to permission error: {dir_path}")

            return result

        def _paths_to_bytes(file_paths: list) -> bytes:
            """Convert files to bytes"""
            _bytes = io.BytesIO()
            for file_path in file_paths:
                try:
                    with open(file_path, 'rb') as f:
                        _bytes.write(f.read())
                except Exception as e:
                    print(f"Failed to read file: {file_path}, error: {e}")
            return _bytes.getvalue()

        header = _path_to_dict(path)
        header_json = json.dumps(header, sort_keys=True, separators=(',', ':')).encode('utf-8')
        header_string_size = len(header_json)
        data_size = 4
        aligned_size = round_up(header_string_size, data_size)
        header_size = aligned_size + 8
        header_object_size = aligned_size + data_size
        diff = aligned_size - header_string_size
        header_json = header_json + b'\0' * diff if diff else header_json

        fp = io.BytesIO()
        fp.write(struct.pack('<4I', data_size, header_size, header_object_size, header_string_size))
        fp.write(header_json)
        fp.write(_paths_to_bytes(paths))

        return cls(
            path=path,
            fp=fp,
            header=header,
            base_offset=round_up(16 + header_string_size, 4))

    def list_files(self, prefix: str = "") -> list:
        """Return list of files in the ASAR archive"""
        files = []

        def _walk_files(files_dict: Dict[str, Any], current_path: str = ""):
            for name, info in files_dict.items():
                full_path = os.path.join(current_path, name).replace('\\', '/')
                if 'files' in info:
                    files.append(full_path + '/')  # Directory marker
                    _walk_files(info['files'], full_path)
                else:
                    files.append(full_path)

        _walk_files(self.header['files'])
        return [f for f in files if f.startswith(prefix)]

    def extract_file(self, file_path: str) -> bytes:
        """Extract a specific file and return as bytes"""
        def _find_file(files_dict: Dict[str, Any], path_parts: list):
            if not path_parts:
                return None

            name = path_parts[0]
            if name not in files_dict:
                return None

            if len(path_parts) == 1:
                return files_dict[name]
            else:
                if 'files' in files_dict[name]:
                    return _find_file(files_dict[name]['files'], path_parts[1:])
                return None

        path_parts = file_path.replace('\\', '/').split('/')
        file_info = _find_file(self.header['files'], path_parts)

        if not file_info or 'offset' not in file_info:
            raise FileNotFoundError(f"File not found: {file_path}")

        self.fp.seek(self.base_offset + int(file_info['offset']))
        return self.fp.read(int(file_info['size']))

    def get_file_info(self) -> Dict[str, Any]:
        """Return comprehensive information about the ASAR file"""
        return {
            'path': self.path,
            'base_offset': self.base_offset,
            'file_count': len(self.list_files()),
            'header': self.header
        }

    def _copy_unpacked_file(self, source: str, destination: str):
        unpacked_dir = self.path + '.unpacked'
        if not os.path.isdir(unpacked_dir):
            print("Couldn't copy file {}, no extracted directory".format(source))
            return

        src = os.path.join(unpacked_dir, source)
        if not os.path.exists(src):
            print("Couldn't copy file {}, doesn't exist".format(src))
            return

        dest = os.path.join(destination, source)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)

    def _extract_file(self, source: str, info: Dict[str, Any], destination: str):
        if 'offset' not in info:
            self._copy_unpacked_file(source, destination)
            return

        self.fp.seek(self.base_offset + int(info['offset']))
        r = self.fp.read(int(info['size']))

        dest = os.path.join(destination, source)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(r)

    def _extract_link(self, source: str, link: str, destination: str):
        dest_filename = os.path.normpath(os.path.join(destination, source))
        link_src_path = os.path.dirname(os.path.join(destination, link))
        link_to = os.path.join(link_src_path, os.path.basename(link))

        try:
            os.symlink(link_to, dest_filename)
        except OSError as e:
            if e.errno == errno.EEXIST:
                os.unlink(dest_filename)
                os.symlink(link_to, dest_filename)
            else:
                raise e

    def _extract_directory(self, source: str, files: Dict[str, Any], destination: str):
        dest = os.path.normpath(os.path.join(destination, source))

        if not os.path.exists(dest):
            os.makedirs(dest)

        for name, info in files.items():
            item_path = os.path.join(source, name)

            if 'files' in info:
                self._extract_directory(item_path, info['files'], destination)
            elif 'link' in info:
                self._extract_link(item_path, info['link'], destination)
            else:
                self._extract_file(item_path, info, destination)

    def extract(self, path: Union[str, Path]):
        """Extract the entire ASAR archive"""
        path = str(path)
        if os.path.exists(path):
            raise FileExistsError(f"Target path already exists: {path}")
        self._extract_directory('.', self.header['files'], path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.fp.close()


def pack_asar(source: Union[str, Path], dest: Union[str, Path], exclude_patterns: list = None):
    """Compress a directory into an ASAR file"""
    with Asar.compress(source, exclude_patterns) as a:
        with open(str(dest), 'wb') as fp:
            a.fp.seek(0)
            fp.write(a.fp.read())
    print(f"ASAR file created successfully: {dest}")


def extract_asar(source: Union[str, Path], dest: Union[str, Path]):
    """Extract an ASAR file to a directory"""
    with Asar.open(source) as a:
        a.extract(dest)
    print(f"ASAR file extracted successfully: {dest}")


def list_asar_files(asar_path: Union[str, Path]) -> list:
    """Return list of files in the ASAR archive"""
    with Asar.open(asar_path) as a:
        return a.list_files()

def extract_asar_file(asar_path: Union[str, Path], file_path: str, output_path: Union[str, Path]):
    """Extract a specific file from ASAR archive"""
    with Asar.open(asar_path) as a:
        data = a.extract_file(file_path)
        with open(str(output_path), 'wb') as f:
            f.write(data)
    print(f"File extracted successfully: {output_path}")

def show_menu():
    """Display menu"""
    print("\n=== ASAR File Tool ===")
    print("1. Compress directory to ASAR file")
    print("2. Extract ASAR file to directory")
    print("3. List files in ASAR")
    print("4. Extract specific file from ASAR")
    print("5. Show ASAR file information")
    print("0. Exit")
    print("=" * 25)


def get_user_input():
    """Handle user input"""
    while True:
        show_menu()
        try:
            choice = int(input("Select menu option (0-5): "))
            if choice == 0:
                print("Exiting program.")
                break
            elif choice == 1:
                source = input("Directory path to compress: ").strip()
                dest = input("ASAR file path to create(Leave empty for auto-generation): ").strip()
                if not dest:
                    source_path = Path(source)
                    dest = str(source_path.parent / f"{source_path.name}.asar")
                    print(f"Auto-generated output path: {dest}")
                exclude_input = input("Exclude patterns (comma-separated, press enter if none): ").strip()

                exclude_patterns = [p.strip() for p in exclude_input.split(',')] if exclude_input else None

                if os.path.exists(source):
                    pack_asar(source, dest, exclude_patterns)
                else:
                    print(f"Error: Directory not found: {source}")

            elif choice == 2:
                source = input("ASAR file path to extract: ").strip()
                dest = input("Directory path to extract to: ").strip()

                if os.path.exists(source):
                    extract_asar(source, dest)
                else:
                    print(f"Error: ASAR file not found: {source}")

            elif choice == 3:
                asar_path = input("ASAR file path: ").strip()

                if os.path.exists(asar_path):
                    files = list_asar_files(asar_path)
                    print(f"\n=== ASAR File List (Total: {len(files)} files) ===")
                    for i, file in enumerate(files[:50]):  # Show first 50 files only
                        print(f"{i+1:3d}. {file}")
                    if len(files) > 50:
                        print(f"... and {len(files) - 50} more")
                else:
                    print(f"Error: ASAR file not found: {asar_path}")

            elif choice == 4:
                asar_path = input("ASAR file path: ").strip()
                file_path = input("File path to extract: ").strip()
                output_path = input("Output file path: ").strip()

                if os.path.exists(asar_path):
                    try:
                        extract_asar_file(asar_path, file_path, output_path)
                    except FileNotFoundError as e:
                        print(f"Error: {e}")
                else:
                    print(f"Error: ASAR file not found: {asar_path}")

            elif choice == 5:
                asar_path = input("ASAR file path: ").strip()

                if os.path.exists(asar_path):
                    with Asar.open(asar_path) as asar_file:
                        info = asar_file.get_file_info()
                        print(f"\n=== ASAR File Information ===")
                        print(f"File path: {info['path']}")
                        print(f"Base offset: {info['base_offset']}")
                        print(f"Total file count: {info['file_count']}")

                        # Calculate file size
                        file_size = os.path.getsize(asar_path)
                        print(f"File size: {file_size:,} bytes ({file_size / 1024 / 1024:.2f} MB)")
                else:
                    print(f"Error: ASAR file not found: {asar_path}")

            else:
                print("Invalid menu number. Please enter a number between 0-5.")

        except ValueError:
            print("Please enter a number.")
        except KeyboardInterrupt:
            print("\nExiting program.")
            break
        except Exception as e:
            print(f"An error occurred: {e}")

        input("\nPress Enter to continue...")

if __name__ == '__main__':
    get_user_input()