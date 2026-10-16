"""DAO backing ``llm_d_lens.storage.store`` -- see design doc section 5.4.2."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.storage_volume import StorageVolumeRow
from llm_d_lens.storage.contracts import StorageVolume


class StorageVolumeDaoError(Exception):
    """Raised for storage-volume-repository failures that aren't already a domain error."""


class StorageVolumeDao(BaseDao):
    def create(self, volume: StorageVolume) -> StorageVolume:
        with self._transaction() as session:
            if session.get(StorageVolumeRow, volume.id) is not None:
                raise StorageVolumeDaoError(f"storage volume already exists: {volume.id}")
            row = StorageVolumeRow.from_dto(volume)
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                # uq_storage_volumes_cluster_id_name: the name is already taken
                # on this cluster. Surface it as a repository error so callers
                # report a conflict instead of a 500; the Storage API checks
                # for this up front, so reaching here means a concurrent create.
                raise StorageVolumeDaoError(
                    f"a storage volume named '{volume.name}' already exists on cluster {volume.cluster_id}"
                ) from error
            return row.to_dto()

    def get(self, volume_id: str) -> StorageVolume | None:
        with self._read_only() as session:
            row = session.get(StorageVolumeRow, volume_id)
            return row.to_dto() if row else None

    def list(self) -> list[StorageVolume]:
        with self._read_only() as session:
            stmt = select(StorageVolumeRow).order_by(StorageVolumeRow.created_at, StorageVolumeRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, volume: StorageVolume) -> StorageVolume:
        """Full-record upsert-if-exists: replace every mapped column from ``volume``."""
        with self._transaction() as session:
            row = session.get(StorageVolumeRow, volume.id)
            if row is None:
                raise StorageVolumeDaoError(f"storage volume not found: {volume.id}")
            row.sync_from_dto(volume)
            session.flush()
            return row.to_dto()

    def delete(self, volume_id: str) -> None:
        with self._transaction() as session:
            row = session.get(StorageVolumeRow, volume_id)
            if row is not None:
                session.delete(row)
