"""Secret references; persisted configuration never needs secret plaintext."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Protocol

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecretRef:
    scheme: str
    name: str

    @classmethod
    def parse(cls, value: str) -> "SecretRef":
        scheme, separator, name = value.partition(":")
        if separator != ":" or scheme != "env" or not _ENV_NAME.fullmatch(name):
            raise ValueError("secret reference must be env:VALID_ENVIRONMENT_NAME")
        return cls(scheme=scheme, name=name)

    def __str__(self) -> str:
        return f"{self.scheme}:{self.name}"


class SecretResolver(Protocol):
    def resolve(self, reference: SecretRef) -> str: ...


class EnvironmentSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, reference: SecretRef) -> str:
        try:
            return self._environ[reference.name]
        except KeyError as exc:
            raise SecretResolutionError(
                f"environment variable {reference.name!r} is not set"
            ) from exc
