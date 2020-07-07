# (c) Crown copyright Met Office. All rights reserved.
# For further details please refer to the file COPYRIGHT
# which you should have received as part of this distribution
"""
C language handling classes.
"""
import re
from pathlib import Path
from collections import deque

from typing import \
    List, \
    Sequence, \
    Pattern, \
    Generator, \
    Optional, \
    Union, \
    Match, \
    Iterator

from fab.database import \
    DatabaseDecorator, \
    FileInfoDatabase, \
    StateDatabase, \
    WorkingStateException, \
    SqliteStateDatabase

from fab.tasks import \
    SingleFileCommand, \
    TextModifier, \
    TaskException, \
    Analyser

from fab.reader import TextReader, TextReaderDecorator


class CSymbolUnresolvedID(object):
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        if not isinstance(other, CSymbolUnresolvedID):
            message = "Cannot compare CSymbolUnresolvedID with " \
                + other.__class__.__name__
            raise TypeError(message)
        return other.name == self.name


class CSymbolID(CSymbolUnresolvedID):
    def __init__(self, name: str, found_in: Path):
        super().__init__(name)
        self.found_in = found_in

    def __hash__(self):
        return hash(self.name) + hash(self.found_in)

    def __eq__(self, other):
        if not isinstance(other, CSymbolID):
            message = "Cannot compare CSymbolID with " \
                + other.__class__.__name__
            raise TypeError(message)
        return super().__eq__(other) and other.found_in == self.found_in


class CInfo(object):
    def __init__(self,
                 symbol: CSymbolID,
                 depends_on: Sequence[str] = ()):
        self.symbol = symbol
        self.depends_on = list(depends_on)

    def __str__(self):
        return f"C symbol '{self.symbol.name}' " \
            f"from '{self.symbol.found_in}' depending on: " \
            f"{', '.join(self.depends_on)}"

    def __eq__(self, other):
        if not isinstance(other, CInfo):
            message = "Cannot compare C Info with " \
                + other.__class__.__name__
            raise TypeError(message)
        return other.symbol == self.symbol \
            and other.depends_on == self.depends_on

    def add_prerequisite(self, prereq: str):
        self.depends_on.append(prereq)


class CWorkingState(DatabaseDecorator):
    """
    Maintains a database of information relating to C symbols.
    """
    # According to the C standard, section 5.2.4.1,
    # (C11) ISO/IEC 9899, the maximum length of an
    # external identifier is 31 characters.
    #
    _C_LABEL_LENGTH: int = 31

    def __init__(self, database: StateDatabase):
        super().__init__(database)
        create_symbol_table = [
            f'''create table if not exists c_symbol (
                   id integer primary key,
                   symbol character({self._C_LABEL_LENGTH}) not null,
                   found_in character({FileInfoDatabase.PATH_LENGTH})
                       references file_info (filename)
                   )''',
            '''create index if not exists idx_c_symbol
                   on c_symbol (symbol, found_in)'''
        ]
        self.execute(create_symbol_table, {})

        # Although the current symbol will already have been entered into the
        # database it is not necessarily unique. We may have multiple source
        # files which define identically named symbols. Thus it can not be used
        # as a foreign key alone.
        #
        # Meanwhile the dependency symbol may not have been encountered yet so
        # we can't expect it to be in the database. Thus it too may not be
        # used as a foreign key.
        #
        create_prerequisite_table = [
            f'''create table if not exists c_prerequisite (
                id integer primary key,
                symbol character({self._C_LABEL_LENGTH}) not null,
                found_in character({FileInfoDatabase.PATH_LENGTH}) not null,
                prerequisite character({self._C_LABEL_LENGTH}) not null,
                foreign key (symbol, found_in)
                references c_symbol (symbol, found_in)
                )'''
        ]
        self.execute(create_prerequisite_table, {})

    def __iter__(self) -> Generator[CInfo, None, None]:
        """
        Yields all symbols and their containing file names.

        :return: Object per symbol.
        """
        query = '''select s.symbol as name, s.found_in, p.prerequisite as prereq
                   from c_symbol as s
                   left join c_prerequisite as p
                   on p.symbol = s.symbol and p.found_in = s.found_in
                   order by s.symbol, s.found_in, p.prerequisite'''
        rows = self.execute([query], {})
        info: Optional[CInfo] = None
        key: CSymbolID = CSymbolID('', Path())
        for row in rows:
            if CSymbolID(row['name'], Path(row['found_in'])) == key:
                if info is not None:
                    info.add_prerequisite(row['prereq'])
            else:  # (row['name'], row['found_in']) != key
                if info is not None:
                    yield info
                key = CSymbolID(row['name'], Path(row['found_in']))
                info = CInfo(key)
                if row['prereq']:
                    info.add_prerequisite(row['prereq'])
        if info is not None:  # We have left-overs
            yield info

    def add_c_symbol(self, symbol: CSymbolID) -> None:
        """
        Creates a record of a new symbol and the file it is found in.

        Note that the filename is absolute meaning that if you rename or move
        the source directory nothing will match up.

        :param symbol: symbol identifier.
        """
        add_symbol = [
            '''insert into c_symbol (symbol, found_in)
                   values (:symbol, :filename)'''
        ]
        self.execute(add_symbol,
                     {'symbol': symbol.name,
                      'filename': str(symbol.found_in)})

    def add_c_dependency(self,
                         symbol: CSymbolID,
                         depends_on: str) -> None:
        """
        Records the dependency of one symbol on another.

        :param symbol: symbol identifier.
        :param depends_on: Name of the prerequisite symbol.
        """
        add_dependency = [
            '''insert into c_prerequisite(symbol, found_in, prerequisite)
                   values (:symbol, :found_in, :depends_on)'''
        ]
        self.execute(add_dependency, {'symbol': symbol.name,
                                      'found_in': str(symbol.found_in),
                                      'depends_on': depends_on})

    def remove_c_file(self, filename: Union[Path, str]) -> None:
        """
        Removes all records relating of a particular source file.

        :param filename: File to be removed.
        """
        remove_file = [
            '''delete from c_prerequisite
               where found_in = :filename''',
            '''delete from c_symbol where found_in=:filename'''
            ]
        self.execute(remove_file, {'filename': str(filename)})

    def get_symbol(self, name: str) -> List[CInfo]:
        """
        Gets the details of symbols given their name.

        It is possible that identically named symbols appear in multiple
        files, hence why a list is returned. It would be an error to try
        linking these into a single executable but that is not a concern for
        the model of the source tree.

        :param name: symbol name.
        :return: List of symbol information objects.
        """
        query = '''select s.symbol, s.found_in, p.prerequisite
                   from c_symbol as s
                   left join c_prerequisite as p
                   on p.symbol = s.symbol and p.found_in = s.found_in
                   where s.symbol=:symbol
                   order by s.symbol, s.found_in, p.prerequisite'''
        rows = self.execute(query, {'symbol': name})
        info_list: List[CInfo] = []
        previous_id = None
        info: Optional[CInfo] = None
        for row in rows:
            symbol_id = CSymbolID(row['symbol'], Path(row['found_in']))
            if previous_id is not None and symbol_id == previous_id:
                if info is not None:
                    info.add_prerequisite(row['prerequisite'])
            else:  # symbol_id != previous_id
                if info is not None:
                    info_list.append(info)
                info = CInfo(symbol_id)
                if row['prerequisite'] is not None:
                    info.add_prerequisite((row['prerequisite']))
                previous_id = symbol_id
        if info is not None:  # We have left overs
            info_list.append(info)
        if len(info_list) == 0:
            message = 'symbol "{symbol}" not found in database.'
            raise WorkingStateException(message.format(symbol=name))
        return info_list

    def depends_on(self, symbol: CSymbolID)\
            -> Generator[CSymbolID, None, None]:
        """
        Gets the prerequisite symbols of a symbol.

        :param symbol: symbol identifier.
        :return: Prerequisite symbol names. May be an empty list.
        """
        query = '''select p.prerequisite, f.found_in
                   from c_prerequisite as p
                   left join c_symbol as f on f.symbol = p.prerequisite
                   where p.symbol=:symbol and p.found_in=:filename
                   order by p.symbol, f.found_in'''
        rows = self.execute(query, {'symbol': symbol.name,
                                    'filename': str(symbol.found_in)})
        for row in rows:
            if row['found_in'] is None:
                yield CSymbolUnresolvedID(row['prerequisite'])
            else:  # row['found_in'] is not None
                yield CSymbolID(row['prerequisite'], Path(row['found_in']))


class CPragmaInjector(TextReaderDecorator):
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


class CIncludeMarker(TextModifier):
    """
    The task which applies the CPragmaInjector to a C source
    file to inject special #pragmas
    """
    def __init__(self, workspace: Path, reader: TextReader) -> None:
        injector = CPragmaInjector(reader)
        super().__init__(workspace, injector)

    @property
    def extension(self) -> str:
        current = self._reader.filename.suffix
        return f"{current}-fab-marked"


class CPreProcessor(SingleFileCommand):

    @property
    def as_list(self) -> List[str]:
        base_command = ['cpp', '-P']
        file_args = [str(self._filename), str(self.output[0])]
        return base_command + self._flags + file_args

    @property
    def output(self) -> List[Path]:
        return [self._workspace /
                self._filename.with_suffix('.c-fab-pp').name]


class CAnalyser(Analyser):
    def __init__(self, reader: TextReader, database: SqliteStateDatabase):
        super().__init__(reader, database)

    def run(self):
        state = CWorkingState(self.database)
        state.remove_c_file(self._reader.filename)

        index = clang.cindex.Index.create()
        translation_unit = index.parse(self._reader.filename,
                                       args=["-xc"])

        # First find out the extents of any FAB pragmas
        sys_includes = {"start": [], "end": []}
        usr_includes = {"start": [], "end": []}

        # Use a deque to implement a rolling window of 4 identifiers
        # (enough to be sure we can spot an entire pragma)
        identifiers = deque([])
        for token in translation_unit.cursor.get_tokens():
            identifiers.append(token)
            if len(identifiers) < 4:
                continue
            if len(identifiers) > 4:
                identifiers.popleft()

            # Trigger off of the FAB identifier only to save
            # on joining the group too frequently
            if identifiers[2].spelling == "FAB":
                line = identifiers[2].location.line
                full = " ".join(id.spelling for id in identifiers)
                if full == "# pragma FAB SysIncludeStart":
                    sys_includes["start"].append(line)
                elif full == "# pragma FAB SysIncludeEnd":
                    sys_includes["end"].append(line)
                elif full == "# pragma FAB UsrIncludeStart":
                    usr_includes["start"].append(line)
                elif full == "# pragma FAB UsrIncludeEnd":
                    usr_includes["end"].append(line)

        # Now walk the actual nodes and find all relevant external symbols
        for node in translation_unit.cursor.walk_preorder():
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
                if (node.is_definition()
                        and node.linkage == clang.cindex.LinkageKind.EXTERNAL):
                    # A function defined in this source file that is
                    # made available to the rest of the application
                    # - this needs to go in the database
                    pass
                else:
                    # Other declarations are coming from headers
                    # - we need to identify if they come from a system or
                    #   a user header; this will later be cross-referenced
                    #   with any calls we find in this file; if it is
                    #   declared in a system header we can ignore it, but
                    #   if it comes from a user header we need to enter
                    #   it as a dependency in the database
                    pass
