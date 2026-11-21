"""Lexer/parser for the normative VFlow DSL (§8).

The parser produces a small AST with source positions. It does not execute or
validate node semantics; that is the compiler's job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import Diagnostic, ParseError

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
PUNCTUATION = set("{}(),;:[]=.")


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    line: int
    column: int


@dataclass(frozen=True)
class Identifier:
    """An unquoted identifier used where the grammar allows a typed reference."""

    value: str


@dataclass(frozen=True)
class StepAST:
    name: str
    dsl_type: str
    args: dict[str, Any]
    line: int
    column: int


@dataclass(frozen=True)
class Endpoint:
    name: str
    port: str | None = None
    index: int | None = None
    line: int = 1
    column: int = 1


@dataclass(frozen=True)
class EdgeAST:
    endpoints: tuple[Endpoint, ...]
    line: int
    column: int


@dataclass(frozen=True)
class WorkflowAST:
    name: str
    version: int
    input_kind: str
    steps: tuple[StepAST, ...] = field(default_factory=tuple)
    edges: tuple[EdgeAST, ...] = field(default_factory=tuple)


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        while True:
            self._skip_trivia()
            if self.pos >= len(self.source):
                self.tokens.append(Token("EOF", None, self.line, self.column))
                return self.tokens

            char = self.source[self.pos]
            line, column = self.line, self.column

            if char == '"':
                self.tokens.append(self._string())
            elif char.isdigit() or (
                char == "-" and self.pos + 1 < len(self.source)
                and self.source[self.pos + 1].isdigit()
            ):
                self.tokens.append(self._number())
            elif char.isalpha() or char == "_":
                self.tokens.append(self._identifier())
            elif self.source.startswith("->", self.pos):
                self._advance(2)
                self.tokens.append(Token("ARROW", "->", line, column))
            elif char in PUNCTUATION:
                self._advance(1)
                self.tokens.append(Token(char, char, line, column))
            else:
                raise self._error(
                    "LEX_UNEXPECTED_CHARACTER",
                    f"Unexpected character {char!r}",
                    line,
                    column,
                )

    def _skip_trivia(self) -> None:
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char.isspace():
                self._advance(1)
                continue
            if self.source.startswith("//", self.pos) or char == "#":
                while self.pos < len(self.source) and self.source[self.pos] != "\n":
                    self._advance(1)
                continue
            break

    def _advance(self, count: int) -> None:
        for _ in range(count):
            if self.pos >= len(self.source):
                return
            if self.source[self.pos] == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.pos += 1

    def _string(self) -> Token:
        line, column = self.line, self.column
        start = self.pos
        self._advance(1)
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == "\\":
                self._advance(2)
                continue
            if char == '"':
                self._advance(1)
                raw = self.source[start:self.pos]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise self._error(
                        "LEX_INVALID_STRING",
                        f"Invalid JSON string: {exc.msg}",
                        line,
                        column,
                    ) from exc
                return Token("STRING", value, line, column)
            self._advance(1)
        raise self._error("LEX_UNTERMINATED_STRING", "Unterminated string", line, column)

    def _number(self) -> Token:
        line, column = self.line, self.column
        match = NUMBER_RE.match(self.source, self.pos)
        if not match:
            raise self._error("LEX_INVALID_NUMBER", "Invalid number", line, column)
        raw = match.group(0)
        self._advance(len(raw))
        value = float(raw) if any(marker in raw for marker in ".eE") else int(raw)
        return Token("NUMBER", value, line, column)

    def _identifier(self) -> Token:
        line, column = self.line, self.column
        match = IDENT_RE.match(self.source, self.pos)
        assert match is not None
        value = match.group(0)
        self._advance(len(value))
        return Token("IDENT", value, line, column)

    @staticmethod
    def _error(code: str, message: str, line: int, column: int) -> ParseError:
        return ParseError([Diagnostic(code, message, line=line, column=column)])


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = Lexer(source).tokenize()
        self.index = 0

    def parse(self) -> WorkflowAST:
        self._expect_ident("workflow")
        name = self._expect("STRING").value
        self._expect("{")

        version: int | None = None
        input_kind: str | None = None
        steps: list[StepAST] = []
        edges: list[EdgeAST] = []

        while not self._accept("}"):
            if self._accept(";"):
                continue
            if self._peek_ident("version"):
                version = self._parse_version()
            elif self._peek_ident("input"):
                input_kind = self._parse_input()
            elif self._peek_ident("step"):
                steps.append(self._parse_step())
            elif self._peek().kind == "EOF":
                raise self._error(
                    "PARSE_UNTERMINATED_WORKFLOW",
                    "Workflow block is not closed",
                    self._peek(),
                )
            else:
                edges.append(self._parse_edge())

        self._expect("EOF")
        if version is None:
            raise self._error("PARSE_MISSING_VERSION", "Workflow version is required", self._peek())
        if input_kind is None:
            raise self._error("PARSE_MISSING_INPUT", "Workflow input is required", self._peek())
        return WorkflowAST(
            name=name,
            version=version,
            input_kind=input_kind,
            steps=tuple(steps),
            edges=tuple(edges),
        )

    def _parse_version(self) -> int:
        self._expect_ident("version")
        self._expect(":")
        token = self._expect("NUMBER")
        if not isinstance(token.value, int) or token.value < 1:
            raise self._error(
                "PARSE_INVALID_VERSION",
                "Workflow version must be a positive integer",
                token,
            )
        self._accept(";")
        return token.value

    def _parse_input(self) -> str:
        self._expect_ident("input")
        self._expect(":")
        token = self._peek()
        if token.kind in ("IDENT", "STRING"):
            self._advance()
            value = token.value
        else:
            raise self._error(
                "PARSE_INVALID_INPUT",
                "Workflow input must be a type name such as json",
                token,
            )
        self._accept(";")
        return value

    def _parse_step(self) -> StepAST:
        start = self._expect_ident("step")
        name_token = self._expect("IDENT")
        self._expect("=")
        type_parts = [str(self._expect("IDENT").value)]
        while self._accept("."):
            type_parts.append(str(self._expect("IDENT").value))
        dsl_type = ".".join(type_parts)
        self._expect("(")
        args = self._parse_arguments()
        self._expect(")")
        self._accept(";")
        return StepAST(
            name=str(name_token.value),
            dsl_type=dsl_type,
            args=args,
            line=start.line,
            column=start.column,
        )

    def _parse_arguments(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if self._peek().kind == ")":
            return args
        while True:
            key_token = self._peek()
            if key_token.kind not in ("IDENT", "STRING"):
                raise self._error(
                    "PARSE_EXPECTED_ARGUMENT_NAME",
                    "Expected argument name",
                    key_token,
                )
            self._advance()
            key = str(key_token.value)
            self._expect(":")
            if key in args:
                raise self._error(
                    "PARSE_DUPLICATE_ARGUMENT",
                    f"Duplicate argument: {key}",
                    key_token,
                )
            args[key] = self._parse_value()
            if not self._accept(","):
                return args

    def _parse_value(self) -> Any:
        token = self._peek()
        if token.kind == "STRING":
            self._advance()
            return token.value
        if token.kind == "NUMBER":
            self._advance()
            return token.value
        if token.kind == "IDENT":
            self._advance()
            if token.value == "true":
                return True
            if token.value == "false":
                return False
            if token.value == "null":
                return None
            return Identifier(str(token.value))
        if token.kind == "{":
            return self._parse_object()
        if token.kind == "[":
            return self._parse_array()
        raise self._error("PARSE_EXPECTED_VALUE", "Expected a value", token)

    def _parse_object(self) -> dict[str, Any]:
        self._expect("{")
        value: dict[str, Any] = {}
        if self._accept("}"):
            return value
        while True:
            key_token = self._peek()
            if key_token.kind not in ("IDENT", "STRING"):
                raise self._error(
                    "PARSE_EXPECTED_OBJECT_KEY",
                    "Expected object key",
                    key_token,
                )
            self._advance()
            key = str(key_token.value)
            self._expect(":")
            value[key] = self._parse_value()
            if not self._accept(","):
                break
        self._expect("}")
        return value

    def _parse_array(self) -> list[Any]:
        self._expect("[")
        value: list[Any] = []
        if self._accept("]"):
            return value
        while True:
            value.append(self._parse_value())
            if not self._accept(","):
                break
        self._expect("]")
        return value

    def _parse_edge(self) -> EdgeAST:
        first = self._parse_endpoint()
        endpoints = [first]
        while self._accept("ARROW"):
            endpoints.append(self._parse_endpoint())
        if len(endpoints) < 2:
            raise self._error(
                "PARSE_INCOMPLETE_EDGE",
                "An edge chain must contain at least two endpoints",
                self._peek(),
            )
        self._accept(";")
        return EdgeAST(tuple(endpoints), first.line, first.column)

    def _parse_endpoint(self) -> Endpoint:
        token = self._expect("IDENT")
        port: str | None = None
        index: int | None = None
        if self._accept("."):
            port = str(self._expect("IDENT").value)
        elif self._accept("["):
            index_token = self._expect("NUMBER")
            if not isinstance(index_token.value, int) or index_token.value < 0:
                raise self._error(
                    "PARSE_INVALID_BRANCH_INDEX",
                    "Branch index must be a non-negative integer",
                    index_token,
                )
            index = index_token.value
            self._expect("]")
        return Endpoint(
            name=str(token.value),
            port=port,
            index=index,
            line=token.line,
            column=token.column,
        )

    def _peek(self) -> Token:
        return self.tokens[self.index]

    def _advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind != "EOF":
            self.index += 1
        return token

    def _accept(self, kind: str) -> bool:
        if self._peek().kind == kind:
            self._advance()
            return True
        return False

    def _expect(self, kind: str) -> Token:
        token = self._peek()
        if token.kind != kind:
            raise self._error(
                "PARSE_UNEXPECTED_TOKEN",
                f"Expected {kind}, got {token.kind}",
                token,
            )
        return self._advance()

    def _peek_ident(self, value: str) -> bool:
        token = self._peek()
        return token.kind == "IDENT" and token.value == value

    def _expect_ident(self, value: str) -> Token:
        token = self._peek()
        if token.kind != "IDENT" or token.value != value:
            raise self._error(
                "PARSE_UNEXPECTED_TOKEN",
                f"Expected {value!r}",
                token,
            )
        return self._advance()

    @staticmethod
    def _error(code: str, message: str, token: Token) -> ParseError:
        return ParseError(
            [Diagnostic(code, message, line=token.line, column=token.column)]
        )


def parse(source: str) -> WorkflowAST:
    return Parser(source).parse()
