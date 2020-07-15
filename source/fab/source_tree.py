##############################################################################
# (c) Crown copyright Met Office. All rights reserved.
# For further details please refer to the file COPYRIGHT
# which you should have received as part of this distribution
##############################################################################
"""
Descend a directory tree or trees processing source files found along the way.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping, List, Union, Type, Callable, Tuple
from re import match

from fab.database import SqliteStateDatabase
from fab.tasks import \
    Task, \
    Analyser, \
    Command, \
    TextModifier, \
    SingleFileCommand
from fab.tasks.common import \
    CommandTask
from fab.reader import TextReader, FileTextReader


class TreeVisitor(ABC):
    @abstractmethod
    def visit(self, candidate: Path) -> List[Path]:
        raise NotImplementedError("Abstract method must be implemented")


class SourceVisitor(TreeVisitor):
    def __init__(self,
                 source_map:
                     List[Tuple[str, Union[Type[Task], Type[Command]]]],
                 command_flags_map: Mapping[Type[Command], List[str]],
                 state: SqliteStateDatabase,
                 workspace: Path,
                 task_handler: Callable):
        self._source_map = source_map
        self._command_flags_map = command_flags_map
        self._state = state
        self._workspace = workspace
        self._task_handler = task_handler

    def visit(self, candidate: Path) -> List[Path]:
        new_candidates: List[Path] = []

        task_class = None
        for pattern, classname in self._source_map:
            # Note we keep searching through the map
            # even after a match is found; this means that
            # later matches will override earlier ones
            if match(pattern, str(candidate)):
                task_class = classname

        if task_class is None:
            return new_candidates

        reader: TextReader = FileTextReader(candidate.resolve())

        if issubclass(task_class, Analyser):
            task: Task = task_class(reader, self._state)
        elif issubclass(task_class, SingleFileCommand):
            flags = self._command_flags_map.get(task_class, [])
            task = CommandTask(
                task_class(Path(reader.filename), self._workspace, flags))
        elif issubclass(task_class, TextModifier):
            task = task_class(self._workspace, reader)
        else:
            message = \
                f"Unhandled class '{task_class}' in extension map."
            raise TypeError(message)

        self._task_handler(task)

        new_candidates.extend(task.products)
        return new_candidates


class CoreLinker(TreeVisitor):
    def __init__(self, core_dir: Path, extensions: List[str]):
        self._core_dir = core_dir
        self._extensions = extensions

    def visit(self, candidate: Path):
        if candidate.suffix in self._extensions:
            link = self._core_dir / candidate.name
            if link.exists():
                link.unlink()
            link.symlink_to(candidate.absolute())
        return []


class TreeDescent(object):
    def __init__(self, root: Path):
        self._root = root

    def descend(self, visitor: TreeVisitor):
        to_visit = [self._root]
        while len(to_visit) > 0:
            candidate: Path = to_visit.pop()
            if candidate.is_dir():
                to_visit.extend(sorted(candidate.iterdir()))
                continue

            # At this point the object should be a file, directories having
            # been dealt with previously.
            #
            to_visit.extend(visitor.visit(candidate))
