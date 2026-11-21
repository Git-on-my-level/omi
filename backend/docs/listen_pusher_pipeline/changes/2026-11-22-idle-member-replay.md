# Recover idle episodes during capture-end finalization replay

An ended legacy member outside the Omi-source cohort could keep yielding
`live_episode_notes` after its owning episode was idle. The existing
Omi-source recovery and paid age fallback shared an eligibility check.

A leased non-Omi capture-end retry can now attempt the ordinary idle close
after the episode reaches the existing quiet gap. The same Firestore
transaction checks the member link, episode version and activity, current
finalization generation and lease, account deletion marker, and conversation
lifecycle and custody before closing. A lost fence leaves the job on normal
replay. Closing uses the existing composition schedule and does not grant an
additional paid live-member fallback.

The existing `HOME_LIVE_MEMBER_NOTES_BOUNDED_ENABLED` kill switch covers this
path. Production proof for the fixed cohort is tracked in SCA-3590.
