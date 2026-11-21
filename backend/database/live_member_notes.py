"""One automatic live-member fallback attempt per finalization/content revision.

The permit is consumed before paid work. A crash or provider failure cannot buy
it again; episode closure or explicit user processing exits this extra budget.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from google.cloud import firestore

from database._client import get_firestore_client
from database.account_deletion_policy import account_deletion_marker_blocks_access
from database.batch_account_authority_schema import ACCOUNT_DELETION_COLLECTION
from database.conversation_finalization_schema import FINALIZATION_JOBS_COLLECTION
from models.finalization_owner import python_owns_finalization


def eligible_idle_replay_member(row: Mapping[str, Any]) -> bool:
    source = row.get('source')
    return (
        row.get('status') == 'processing'
        and isinstance(source, str)
        and bool(source)
        and source != 'omi'
        and python_owns_finalization(row)
        and not any(
            row.get(field)
            for field in (
                'deleted',
                'discarded',
                'deferred',
                'is_locked',
                'sync_merged_into',
                'record_lane_owner',
                'recording',
                'v3_derived',
                'v3_pending',
                'v3_backfill_process_pending',
            )
        )
    )


@dataclass(frozen=True)
class IdleMemberClose:
    """An idle close authorized by a specific leased capture-end retry."""

    conversation_id: str
    job_id: str
    dispatch_generation: int
    lease_epoch: int
    episode_version: int

    def authorize(self, transaction, client, uid, episode_id, episode) -> tuple[dict, datetime] | None:
        members = episode.get('conversation_ids')
        if (
            type(self.episode_version) is not int
            or episode.get('state_version') != self.episode_version
            or episode.get('recorded_merged_into')
            or episode.get('recovered_conversation_ids')
            or not isinstance(members, list)
            or self.conversation_id not in members
        ):
            return None
        user = client.collection('users').document(uid)
        link = user.collection('episode_links').document(self.conversation_id).get(transaction=transaction)
        if (link.to_dict() or {}).get('episode_id') != episode_id:
            return None
        job_ref = client.collection(FINALIZATION_JOBS_COLLECTION).document(self.job_id)
        job = job_ref.get(transaction=transaction).to_dict() or {}
        expires = job.get('lease_expires_at')
        if (
            job.get('uid') != uid
            or job.get('conversation_id') != self.conversation_id
            or job.get('status') != 'leased'
            or job.get('dispatch_generation') != self.dispatch_generation
            or job.get('lease_epoch') != self.lease_epoch
            or not isinstance(expires, datetime)
            or expires <= datetime.now(timezone.utc)
            or job.get('requires_byok')
            or job.get('processing_trigger') != 'capture_end'
            or job.get('last_failure_code') != 'live_episode_notes'
            or not python_owns_finalization(job)
        ):
            return None
        member = user.collection('conversations').document(self.conversation_id).get(transaction=transaction)
        row = member.to_dict() or {}
        revision = job.get('finalization_revision')
        if (
            not eligible_idle_replay_member(row)
            or row.get('finalization_job_id') != self.job_id
            or type(revision) is not int
            or revision < 1
            or row.get('finalization_revision') != revision
        ):
            return None
        marker = client.collection(ACCOUNT_DELETION_COLLECTION).document(uid).get(transaction=transaction)
        if account_deletion_marker_blocks_access(
            marker_exists=marker.exists, raw_status=(marker.to_dict() or {}).get('wipe_status')
        ):
            return None
        return row, expires


def _claim(transaction, job_ref, conversation_ref, fence, revision, now):
    job = job_ref.get(transaction=transaction).to_dict() or {}
    row = conversation_ref.get(transaction=transaction).to_dict() or {}
    identity = {'finalization': job.get('finalization_revision'), 'content': revision}
    if (
        job.get('status') != 'leased'
        or (job.get('dispatch_generation'), job.get('lease_epoch')) != fence[1:]
        or not job.get('lease_expires_at')
        or job['lease_expires_at'] <= now
        or job.get('uid') != conversation_ref.parent.parent.id
        or job.get('conversation_id') != conversation_ref.id
        or row.get('finalization_job_id') != fence[0]
        or row.get('finalization_revision') != identity['finalization']
        or row.get('sync_content_revision') != revision
        or row.get('status') != 'processing'
        or any(row.get(k) for k in ('deleted', 'discarded', 'sync_merged_into'))
        or row.get('live_member_notes_fallback_revision') == identity
    ):
        return False
    transaction.update(conversation_ref, {'live_member_notes_fallback_revision': identity})
    return True


def claim(uid, conversation_id, fence, revision, *, firestore_client=None):
    client = firestore_client if firestore_client is not None else get_firestore_client()
    return firestore.transactional(_claim)(
        client.transaction(),
        client.collection('conversation_finalization_jobs').document(fence[0]),
        client.collection('users').document(uid).collection('conversations').document(conversation_id),
        fence,
        revision,
        datetime.now(timezone.utc),
    )
