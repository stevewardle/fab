# (c) Crown copyright Met Office. All rights reserved.
# For further details please refer to the file COPYRIGHT
# which you should have received as part of this distribution
"""
C language handling classes.
"""
import re
from pathlib import Path

from typing import \
    List, \
    Pattern, \
    Optional, \
    Match, \
    Iterator

from fab.tasks import \
    TextModifier, \
    TaskException, \
    SingleFileCommand

from fab.reader import TextReader, TextReaderDecorator


class CTextReaderPragmas(TextReaderDecorator):
    """
    Reads a C source file but when encountering an #include
    preprocessor directive injects a special Fab-specific
    #pragma which can be picked up later by the Analyser
    after the preprocessing
    """
    def __init__(self, source: TextReader):
        super().__init__(source)
        self._line_buffer = ''

    _include_re: str = r'^\s*#include\s+(\S+)'
    _include_pattern: Pattern = re.compile(_include_re)

    def line_by_line(self) -> Iterator[str]:
        for line in self._source.line_by_line():
            include_match: Optional[Match] \
                = self._include_pattern.match(line)
            if include_match:
                # For valid C the first character of the matched
                # part of the group will indicate whether this is
                # a system library include or a user include
                include: str = include_match.group(1)
                # TODO: Is this sufficient?  Or do the pragmas
                #       need to include identifying info
                #       e.g. the name of the original include?
                if include.startswith('<'):
                    yield '#pragma FAB SysIncludeStart\n'
                    yield line
                    yield '#pragma FAB SysIncludeEnd\n'
                elif include.startswith(('"', "'")):
                    yield '#pragma FAB UsrIncludeStart\n'
                    yield line
                    yield '#pragma FAB UsrIncludeEnd\n'
                else:
                    msg = 'Found badly formatted #include'
                    raise TaskException(msg)
            else:
                yield line


class CPragmaInjector(TextModifier):
    """
    The task which applies the CPragmaInjector to a C source
    file to inject special #pragmas
    """
    def __init__(self, workspace: Path, reader: TextReader) -> None:
        self._injector = CTextReaderPragmas(reader)
        super().__init__(workspace, self._injector)

    @property
    def products(self) -> List[Path]:
        input_file = self._injector.filename
        if isinstance(input_file, Path):
            return [self._workspace / input_file.name]
        else:
            return []


class CPreProcessor(SingleFileCommand):

    @property
    def as_list(self) -> List[str]:
        base_command = ['cpp', '-P']
        file_args = [str(self._filename), str(self.output[0])]
        return base_command + self._flags + file_args

    @property
    def output(self) -> List[Path]:
        return [self._workspace /
                self._filename.with_suffix('.c').name]
