"""Strict boundary validation: never coerce model output into approvals."""
import math

class ContractError(ValueError):
    pass

def obj(value, name):
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value

def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a nonempty string")
    return value

def sequence(value, name):
    if not isinstance(value, list):
        raise ContractError(f"{name} must be an array")
    return value

def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name} must be an integer >= {minimum}")
    return value

def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ContractError(f"{name} must be finite numeric")
    return value

def unique_strings(value, name, nonempty=True):
    result = [text(x, name) for x in sequence(value, name)]
    if (nonempty and not result) or len(result) != len(set(result)):
        raise ContractError(f"{name} must contain unique strings" + (" and not be empty" if nonempty else ""))
    return result

def enum(value, name, choices):
    if value not in choices or not isinstance(value, str):
        raise ContractError(f"{name} must be one of {sorted(choices)}")
    return value

def verdict(accepted, reasons, **extra):
    return {"accepted": accepted, "reasons": reasons, **extra}
