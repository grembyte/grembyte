"""Favorites: C# methods and native functions marked to come back to later.

The catalog, the Mono trace and the native trace find interesting code, but
their results are gone when the window closes or the game freezes or
crashes. A favorite is saved at once, per program, and outlives the process:
a C# method is found again by its assembly and signature, a native function
by its module and offset. Code outside any module (generated at run time)
only has an address, which the next start of the game changes.
"""

from dataclasses import asdict, dataclass

from hermes.features.catalog import CatalogMethod, short_name
from hermes.features.native_trace import NativeFunction


METHOD = "method"
NATIVE = "native"

# Where a favorite was marked
CATALOG = "catalog"
TRACE = "trace"
NATIVE_TRACE = "native_trace"


def method_key(method):
    """What tells a CatalogMethod apart from others, across starts of the game."""
    return f"{METHOD}:{method.engine}:{method.assembly}:{method.signature}"


def function_key(function):
    """What tells a NativeFunction apart from others; its address only outside modules."""
    if function.module:
        return f"{NATIVE}:{function.module}+0x{function.offset:x}"
    return f"{NATIVE}:0x{function.address:x}"


def favorite_key(item):
    """The key of a CatalogMethod or a NativeFunction."""
    return function_key(item) if isinstance(item, NativeFunction) else method_key(item)


@dataclass
class Favorite:
    """A saved method or function; ``data`` holds the fields to rebuild it."""

    # pylint: disable=too-many-instance-attributes
    kind: str
    key: str
    label: str
    location: str
    signature: str
    data: dict
    note: str = ""
    source: str = ""
    favorite_id: int | None = None
    created_at: object = None

    @property
    def lasting(self):
        """Whether the favorite still points to the same code after a restart."""
        return self.kind == METHOD or bool(self.data.get("module"))

    def method(self):
        """The CatalogMethod of a METHOD favorite."""
        return CatalogMethod(**self.data)

    def function(self):
        """The NativeFunction of a NATIVE favorite."""
        return NativeFunction(**self.data)

    def target(self):
        """The CatalogMethod or NativeFunction to monitor."""
        return self.method() if self.kind == METHOD else self.function()

    def matches(self, words):
        """Whether every search word occurs in name, location, signature or note."""
        text = f"{self.label} {self.location} {self.signature} {self.note}".lower()
        return all(word in text for word in words)


def favorite_from_method(method, source=""):
    """A Favorite of a CatalogMethod."""
    return Favorite(METHOD, method_key(method), short_name(method.signature),
                    method.assembly, method.signature, asdict(method), source=source)


def favorite_from_function(function, source=""):
    """A Favorite of a NativeFunction."""
    return Favorite(NATIVE, function_key(function), function.label, function.location,
                    function.signature, asdict(function), source=source)


def favorite_of(item, source=""):
    """A Favorite of a CatalogMethod or a NativeFunction."""
    if isinstance(item, NativeFunction):
        return favorite_from_function(item, source)
    return favorite_from_method(item, source)
