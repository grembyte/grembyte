"""Pointer scan ORM model."""

import json
import zlib
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, LargeBinary

from grembyte.db.base import Base


def _now():
    return datetime.now(timezone.utc)


def pack_chains(chains):
    """Chains as ``PointerChain.to_json()`` gives them, compressed for the database.

    A scan keeps up to a million chains. As JSON objects each repeated its
    module name and keys, about 50 MB per scan; here each module is named
    once and a chain is ``[module index, base offset, [offsets]]``.
    """
    modules = {}
    rows = []
    for chain in chains:
        index = modules.setdefault(chain["module"], len(modules))
        rows.append([index, int(chain.get("base_offset", 0)),
                     [int(offset) for offset in chain.get("offsets", ())]])
    text = json.dumps({"modules": list(modules), "chains": rows}, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8"), 6)


def unpack_chains(data):
    """The chains ``pack_chains`` packed, as ``PointerChain.to_json()`` gives them."""
    stored = json.loads(zlib.decompress(data).decode("utf-8"))
    modules = stored["modules"]
    return [{"module": modules[index], "base_offset": base_offset, "offsets": offsets}
            for index, base_offset, offsets in stored["chains"]]


class PointerScan(Base):  # pylint: disable=too-few-public-methods
    """Candidate pointer chains of an address-list entry.

    Kept between restarts of the program so the candidates can be filtered
    with the address the value has in each new run.

    The chains are kept in ``packed`` (see ``pack_chains``). ``chains`` held
    them as JSON before; it stays for older databases, where it is required,
    and is empty once the scan was packed (grembyte.db.engine.pack_pointer_scans).
    """

    __tablename__ = "pointer_scans"

    id = Column(Integer, primary_key=True)
    address_id = Column(
        Integer, ForeignKey("addresses.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    max_depth = Column(Integer, nullable=False)
    max_offset = Column(Integer, nullable=False)
    chains = Column(JSON, nullable=False, default=list)  # unpacked, from older versions
    packed = Column(LargeBinary, nullable=True)         # pack_chains([PointerChain.to_json()])
    chain_count = Column(Integer, nullable=True)        # chains in ``packed``
    rounds = Column(Integer, nullable=False, default=0)  # times filtered after a restart
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def chain_list(self):
        """The chains as ``PointerChain.to_json()`` gives them."""
        if self.packed is not None:
            return unpack_chains(self.packed)
        return list(self.chains or [])

    def set_chains(self, chains):
        """Keep ``chains`` (``PointerChain.to_json()`` each) packed."""
        self.packed = pack_chains(chains)
        self.chain_count = len(chains)
        self.chains = []
