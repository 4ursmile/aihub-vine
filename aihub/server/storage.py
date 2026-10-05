import os
import shutil
from abc import ABC, abstractmethod


class Storage(ABC):
    @abstractmethod
    def put(self, package, filename, src_path): ...
    @abstractmethod
    def path(self, package, filename): ...
    @abstractmethod
    def exists(self, package, filename): ...
    @abstractmethod
    def delete(self, package, filename): ...
    @abstractmethod
    def open(self, package, filename): ...


class LocalStorage(Storage):
    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def path(self, package, filename):
        p = os.path.realpath(os.path.join(self.root, package, os.path.basename(filename)))
        if not p.startswith(self.root + os.sep):
            raise ValueError("bad path")
        return p

    def put(self, package, filename, src_path):
        dst = self.path(package, filename)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src_path, dst)
        return dst

    def exists(self, package, filename):
        return os.path.isfile(self.path(package, filename))

    def delete(self, package, filename):
        if self.exists(package, filename):
            os.remove(self.path(package, filename))

    def open(self, package, filename):
        return open(self.path(package, filename), "rb")
