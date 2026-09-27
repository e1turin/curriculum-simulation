from typing import Any


class IllegalStateError(Exception):
    """Extension which means violated internal contraints or assumptions"""


type Condition = bool | object | None


def require(cond: Condition, msg: str):
    """Declares input constraints"""
    if not cond:
        raise ValueError(msg)


def check(cond: Condition, msg: str):
    """Declares internal constraints"""
    if not cond:
        raise IllegalStateError(msg)


def typecheck(obj: Any, ty: type):
    """Declares type constraints, but better use proper types with match-case statement and not typechecking"""
    if not isinstance(obj, ty):
        raise IllegalStateError(
            f"Object {obj} is supposed to have type {ty} but it is {type(obj)}"
        )
