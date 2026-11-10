"""Persistence of the address list and its pointer scans."""

from dataclasses import dataclass

from sqlalchemy import delete, select

from hermes.core.pointer import PointerChain
from hermes.db.engine import SESSION_LOCAL
from hermes.db.models import Address, PointerScan


_UNSET = object()


@dataclass
class SavedPointerScan:
    """Candidate chains of an address-list entry."""

    chains: list
    max_depth: int
    max_offset: int
    rounds: int
    updated_at: object = None


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
                chains=[PointerChain.from_json(chain) for chain in scan.chains],
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
            stored.chains = [chain.to_json() for chain in scan.chains]
            stored.max_depth = scan.max_depth
            stored.max_offset = scan.max_offset
            stored.rounds = scan.rounds
            session.commit()
