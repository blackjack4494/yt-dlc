# coding: utf-8
from __future__ import unicode_literals, print_function, division

"""
A partial parser for WebVTT segments. Interprets enough of the WebVTT stream
to be able to assemble a single stand-alone subtitle file, suitably adjusting
timestamps on the way, while everything else is passed through unmodified.

Regular expressions based on the W3C WebVTT specification
<https://www.w3.org/TR/webvtt1/>. The X-TIMESTAMP-MAP extension is described
in RFC 8216 §3.5 <https://tools.ietf.org/html/rfc8216#section-3.5>.
"""

import re
import io
from .utils import int_or_none
from .compat import (
    compat_str as str,
    compat_Pattern,
    compat_Match,
)


class _MatchParser(object):
    """
    An object that maintains the current parsing position and allows
    conveniently advancing it as syntax elements are successfully parsed.
    """

    def __init__(self, string):
        self._data = string
        self._pos = 0

    def match(self, r):
        if isinstance(r, compat_Pattern):
            return r.match(self._data, self._pos)
        if isinstance(r, str):
            if self._data.startswith(r, self._pos):
                return len(r)
            return None
        raise ValueError(r)

    def advance(self, by):
        if by is None:
            amt = 0
        elif isinstance(by, compat_Match):
            amt = len(by.group(0))
        elif isinstance(by, str):
            amt = len(by)
        elif isinstance(by, int):
            amt = by
        else:
            raise ValueError(by)
        self._pos += amt
        return by

    def consume(self, r):
        return self.advance(self.match(r))

    def child(self):
        return _MatchChildParser(self)


class _MatchChildParser(_MatchParser):
    """
    A child parser state, which advances through the same data as
    its parent, but has an independent position. This is useful when
    advancing through syntax elements we might later want to backtrack
    from.
    """

    def __init__(self, parent):
        super(_MatchChildParser, self).__init__(parent._data)
        self.__parent = parent
        self._pos = parent._pos

    def commit(self):
        """
        Advance the parent state to the current position of this child state.
        """
        self.__parent._pos = self._pos
        return self.__parent


class ParseError(Exception):
    def __init__(self, parser):
        super(ParseError, self).__init__("Parse error at position %u (near %r)" % (
            parser._pos, parser._data[parser._pos:parser._pos + 20]
        ))


_REGEX_TS = re.compile(r'''(?x)
    (?:([0-9]{2,}):)?
    ([0-9]{2}):
    ([0-9]{2})\.
    ([0-9]{3})?
''')
_REGEX_EOF = re.compile(r'\Z')
_REGEX_NL = re.compile(r'(?:\r\n|[\r\n])')
_REGEX_BLANK = re.compile(r'(?:\r\n|[\r\n])+')


def _parse_ts(ts):
    """
    Convert a parsed WebVTT timestamp (a re.Match obtained from _REGEX_TS)
    into an MPEG PES timestamp: a tick counter at 90 kHz resolution.
    """

    h, min, s, ms = ts.groups()
    return 90 * (
        int(h or 0) * 3600000 +  # noqa: W504,E221,E222
        int(min)    *   60000 +  # noqa: W504,E221,E222
        int(s)      *    1000 +  # noqa: W504,E221,E222
        int(ms)                  # noqa: W504,E221,E222
    )


def _format_ts(ts):
    """
    Convert an MPEG PES timestamp into a WebVTT timestamp.
    This will lose sub-millisecond precision.
    """
    msec = int((ts + 45) // 90)
    secs, msec = divmod(msec, 1000)
    mins, secs = divmod(secs, 60)
    hrs, mins = divmod(mins, 60)
    return '%02u:%02u:%02u.%03u' % (hrs, mins, secs, msec)


class Block(object):
    """
    An abstract WebVTT block.
    """

    def __init__(self, **kwargs):
        for key, val in kwargs.items():
            setattr(self, key, val)

    @classmethod
    def parse(cls, parser):
        m = parser.match(cls._REGEX)
        if not m:
            return None
        parser.advance(m)
        return cls(raw=m.group(0))

    def write_into(self, stream):
        stream.write(self.raw)


class HeaderBlock(Block):
    """
    A WebVTT block that may only appear in the header part of the file,
    i.e. before any cue blocks.
    """

    pass


class Magic(HeaderBlock):
    _REGEX = re.compile(r'\ufeff?WEBVTT([ \t][^\r\n]*)?(?:\r\n|[\r\n])')

    # XXX: The X-TIMESTAMP-MAP extension is described in RFC 8216 §3.5
    # <https://tools.ietf.org/html/rfc8216#section-3.5>, but the RFC
    # doesn’t specify the exact grammar nor where in the WebVTT
    # syntax it should be placed; the below has been devised based
    # on usage in the wild
    #
    # And strictly speaking, the presence of this extension violates
    # the W3C WebVTT spec. Oh well.

    _REGEX_TSMAP = re.compile(r'X-TIMESTAMP-MAP=')
    _REGEX_TSMAP_LOCAL = re.compile(r'LOCAL:')
    _REGEX_TSMAP_MPEGTS = re.compile(r'MPEGTS:([0-9]+)')

    @classmethod
    def __parse_tsmap(cls, parser):
        parser = parser.child()

        while True:
            m = parser.consume(cls._REGEX_TSMAP_LOCAL)
            if m:
                m = parser.consume(_REGEX_TS)
                if m is None:
                    raise ParseError(parser)
                local = _parse_ts(m)
                if local is None:
                    raise ParseError(parser)
            else:
                m = parser.consume(cls._REGEX_TSMAP_MPEGTS)
                if m:
                    mpegts = int_or_none(m.group(1))
                    if mpegts is None:
                        raise ParseError(parser)
                else:
                    raise ParseError(parser)
            if parser.consume(','):
                continue
            if parser.consume(_REGEX_NL):
                break
            raise ParseError(parser)

        parser.commit()
        return local, mpegts

    @classmethod
    def parse(cls, parser):
        parser = parser.child()

        m = parser.consume(cls._REGEX)
        if not m:
            raise ParseError(parser)

        extra = m.group(1)
        local, mpegts = None, None
        if parser.consume(cls._REGEX_TSMAP):
            local, mpegts = cls.__parse_tsmap(parser)
        if not parser.consume(_REGEX_NL):
            raise ParseError(parser)
        parser.commit()
        return cls(extra=extra, mpegts=mpegts, local=local)

    def write_into(self, stream):
        stream.write('WEBVTT')
        if self.extra is not None:
            stream.write(self.extra)
        stream.write('\n')
        if self.local or self.mpegts:
            stream.write('X-TIMESTAMP-MAP=LOCAL:')
            stream.write(_format_ts(self.local if self.local is not None else 0))
            stream.write(',MPEGTS:')
            stream.write(str(self.mpegts if self.mpegts is not None else 0))
            stream.write('\n')
        stream.write('\n')


class StyleBlock(HeaderBlock):
    _REGEX = re.compile(r'''(?x)
        STYLE[\ \t]*(?:\r\n|[\r\n])
        ((?:(?!-->)[^\r\n])+(?:\r\n|[\r\n]))*
        (?:\r\n|[\r\n])
    ''')


class RegionBlock(HeaderBlock):
    _REGEX = re.compile(r'''(?x)
        REGION[\ \t]*
        ((?:(?!-->)[^\r\n])+(?:\r\n|[\r\n]))*
        (?:\r\n|[\r\n])
    ''')


class CommentBlock(Block):
    _REGEX = re.compile(r'''(?x)
        NOTE(?:\r\n|[\ \t\r\n])
        ((?:(?!-->)[^\r\n])+(?:\r\n|[\r\n]))*
        (?:\r\n|[\r\n])
    ''')


class CueBlock(Block):
    """
    A cue block. The payload is not interpreted.
    """

    _REGEX_ID = re.compile(r'((?:(?!-->)[^\r\n])+)(?:\r\n|[\r\n])')
    _REGEX_ARROW = re.compile(r'[ \t]+-->[ \t]+')
    _REGEX_SETTINGS = re.compile(r'[ \t]+((?:(?!-->)[^\r\n])+)')
    _REGEX_PAYLOAD = re.compile(r'[^\r\n]+(?:\r\n|[\r\n])?')

    @classmethod
    def parse(cls, parser):
        parser = parser.child()

        id = None
        m = parser.consume(cls._REGEX_ID)
        if m:
            id = m.group(1)

        m0 = parser.consume(_REGEX_TS)
        if not m0:
            return None
        if not parser.consume(cls._REGEX_ARROW):
            return None
        m1 = parser.consume(_REGEX_TS)
        if not m1:
            return None
        m2 = parser.consume(cls._REGEX_SETTINGS)
        if not parser.consume(_REGEX_NL):
            return None

        start = _parse_ts(m0)
        end = _parse_ts(m1)
        settings = m2.group(1) if m2 is not None else None

        text = io.StringIO()
        while True:
            m = parser.consume(cls._REGEX_PAYLOAD)
            if not m:
                break
            text.write(m.group(0))

        parser.commit()
        return cls(
            id=id,
            start=start, end=end, settings=settings,
            text=text.getvalue()
        )

    def write_into(self, stream):
        if self.id is not None:
            stream.write(self.id)
            stream.write('\n')
        stream.write(_format_ts(self.start))
        stream.write(' --> ')
        stream.write(_format_ts(self.end))
        if self.settings is not None:
            stream.write(' ')
            stream.write(self.settings)
        stream.write('\n')
        stream.write(self.text)
        stream.write('\n')

    @property
    def as_json(self):
        return {
            'id': self.id,
            'start': self.start,
            'end': self.end,
            'text': self.text,
            'settings': self.settings,
        }


def parse_fragment(frag_content):
    """
    A generator that yields (partially) parsed WebVTT blocks when given
    a bytes object containing the raw contents of a WebVTT file.
    """

    parser = _MatchParser(frag_content.decode('utf-8'))

    yield Magic.parse(parser)

    while not parser.match(_REGEX_EOF):
        if parser.consume(_REGEX_BLANK):
            continue

        block = RegionBlock.parse(parser)
        if block:
            yield block
            continue
        block = StyleBlock.parse(parser)
        if block:
            yield block
            continue
        block = CommentBlock.parse(parser)
        if block:
            yield block  # XXX: or skip
            continue

        break

    while not parser.match(_REGEX_EOF):
        if parser.consume(_REGEX_BLANK):
            continue

        block = CommentBlock.parse(parser)
        if block:
            yield block  # XXX: or skip
            continue
        block = CueBlock.parse(parser)
        if block:
            yield block
            continue

        raise ParseError(parser)
