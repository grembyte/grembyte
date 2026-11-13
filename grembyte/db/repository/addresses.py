"""Persistence of the address list and its pointer scans."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from grembyte.core.pointer import PointerChain
from grembyte.db.engine import SESSION_LOCAL, vacuum_db
from grembyte.db.models import Address, PointerScan, Process

# When a pointer scan is probably no longer needed (see obsolete_pointer_scans):
# days since it was last filtered once its entry has a pointer, ...
CHOSEN_KEEP_DAYS = 7
# ... and without one.
STALE_DAYS = 60

EMPTY, CHOSEN, STALE = "empty", "chosen", "stale"


_UNSET = object()


@dataclass
class SavedPointerScan:
    """Candidate chains of an address-list entry."""

    chains: list
    max_depth: int
    max_offset: int
    rounds: int
    updated_at: object = None


@dataclass
class ObsoleteScan:  # pylint: disable=too-many-instance-attributes
    """A saved pointer scan that is probably no longer needed, and why."""

    address_id: int
    label: str
    process: str
    count: int | None   # candidates; None if not known
    size: int           # bytes in the database
    reason: str         # EMPTY, CHOSEN or STALE
    days: int           # since it was last filtered
    updated_at: object = None

    def key(self):
        """Identifies the scan as it is now: filtering or scanning again changes it."""
        return f"{self.address_id}@{self.updated_at}"


def _days_since(moment, now):
    if moment is None:
        return None
    if moment.tzinfo is None:  # SQLite keeps no time zone; stored in UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, (now - moment).days)


def address_pointer(address):
    """Return the PointerChain of a saved address, or None."""
    return None if address.pointer is None else PointerChain.from_json(address.pointer)


class AddressRepository:
    """Store the address list per program."""

    def __init__(self, session_factory=SESSION_LOCAL):
        self._sessions = session_factory

    def list_addresses(self, process_id):
        """Return the saved addresses of a process in creation order."""
        with self._sessions() as session:
            return list(session.scalars(
                select(Address).where(Address.process_id == process_id).order_by(Address.id)
            ))

    def add_address(self, process_id, *, label, value_type, address, pointer=None):
        """Save an address and return its id."""
        with self._sessions() as session:
            entry = Address(
                process_id=process_id,
                label=label,
                value_type=value_type,
                address=address,
                pointer=None if pointer is None else pointer.to_json(),
            )
            session.add(entry)
            session.commit()
            return entry.id

    def update_address(self, address_id, *, label=None, address=None, pointer=_UNSET,
                       frozen=None, frozen_value=_UNSET):
        """Change the given fields; ``pointer=None`` removes the pointer."""
        with self._sessions() as session:
            entry = session.get(Address, address_id)
            if entry is None:
                return
            if label is not None:
                entry.label = label
            if address is not None:
                entry.address = address
            if pointer is not _UNSET:
                entry.pointer = None if pointer is None else pointer.to_json()
            if frozen is not None:
                entry.frozen = frozen
            if frozen_value is not _UNSET:
                entry.frozen_value = frozen_value
            session.commit()

    def delete_address(self, address_id):
        """Delete an address and its pointer scan."""
        with self._sessions() as session:
            session.execute(delete(PointerScan).where(PointerScan.address_id == address_id))
            entry = session.get(Address, address_id)
            if entry is not None:
                session.delete(entry)
            session.commit()

    def get_pointer_scan(self, address_id):
        """Return the SavedPointerScan of an address, or None."""
        with self._sessions() as session:
            scan = session.scalars(
                select(PointerScan).where(PointerScan.address_id == address_id)
            ).first()
            if scan is None:
                return None
            return SavedPointerScan(
                chains=[PointerChain.from_json(chain) for chain in scan.chain_list()],
                max_depth=scan.max_depth,
                max_offset=scan.max_offset,
                rounds=scan.rounds,
                updated_at=scan.updated_at,
            )

    def delete_pointer_scan(self, address_id):
        """Remove the saved pointer scan of an address (the address stays)."""
        with self._sessions() as session:
            session.execute(delete(PointerScan).where(PointerScan.address_id == address_id))
            session.commit()

    def save_pointer_scan(self, address_id, scan):
        """Create or replace the pointer scan of an address."""
        with self._sessions() as session:
            stored = session.scalars(
                select(PointerScan).where(PointerScan.address_id == address_id)
            ).first()
            if stored is None:
                stored = PointerScan(address_id=address_id)
                session.add(stored)
            stored.set_chains([chain.to_json() for chain in scan.chains])
            stored.max_depth = scan.max_depth
            stored.max_offset = scan.max_offset
            stored.rounds = scan.rounds
            session.commit()

    def obsolete_pointer_scans(self, now=None):
        """The saved pointer scans that are probably no longer needed, largest first.

        A scan is obsolete when filtering left no candidates (EMPTY), when its
        entry has a pointer and it was not filtered for ``CHOSEN_KEEP_DAYS``
        (CHOSEN), or when it was not filtered for ``STALE_DAYS`` (STALE).
        """
        now = now or datetime.now(timezone.utc)
        with self._sessions() as session:
            rows = session.execute(
                select(PointerScan.address_id, Address.label, Process.name,
                       PointerScan.chain_count,
                       func.coalesce(func.length(PointerScan.packed), 0)
                       + func.coalesce(func.length(PointerScan.chains), 0),
                       func.coalesce(PointerScan.updated_at, PointerScan.created_at),
                       Address.pointer)
                .join(Address, Address.id == PointerScan.address_id)
                .join(Process, Process.id == Address.process_id)
            ).all()
        obsolete = []
        for address_id, label, process, count, size, updated_at, pointer in rows:
            days = _days_since(updated_at, now)
            if count == 0:
                reason = EMPTY
            elif days is not None and pointer is not None and days >= CHOSEN_KEEP_DAYS:
                reason = CHOSEN
            elif days is not None and days >= STALE_DAYS:
                reason = STALE
            else:
                continue
            obsolete.append(ObsoleteScan(address_id, label, process, count, size or 0,
                                         reason, days or 0, updated_at))
        obsolete.sort(key=lambda scan: scan.size, reverse=True)
        return obsolete

    def delete_pointer_scans(self, address_ids):
        """Remove the saved pointer scans of the addresses; returns how many."""
        address_ids = list(address_ids)
        if not address_ids:
            return 0
        with self._sessions() as session:
            count = session.execute(
                delete(PointerScan).where(PointerScan.address_id.in_(address_ids))).rowcount
            session.commit()
            return count

    def compact(self):
        """Give the space of deleted scans back (VACUUM); returns the sizes before and after."""
        with self._sessions() as session:
            bind = session.get_bind()
        return vacuum_db(bind)
