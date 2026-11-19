"""Write one JSON Schema per event type/version to contracts/events (spec §1A.4).
Run from libs/common: `uv run python ../../scripts/export_event_schemas.py ../../contracts/events`."""

import json
import sys
from pathlib import Path

from cogo_common.events import contracts as c
from cogo_common.events.envelope import EventEnvelope
from cogo_common.events.topology import RoutingKeys as K

EVENTS = {
    K.USER_REGISTERED: c.UserRegistered,
    K.USER_CREATED: c.UserSnapshot,
    K.USER_UPDATED: c.UserSnapshot,
    K.SOCIAL_FOLLOWED: c.SocialFollowed,
    K.SOCIAL_UNFOLLOWED: c.SocialFollowed,
    K.EMAIL_REQUESTED: c.EmailRequested,
    K.JOURNEY_CREATED: c.JourneySummary,
    K.JOURNEY_UPDATED: c.JourneySummary,
    K.JOURNEY_DELETED: c.JourneySummary,
    K.JOURNEY_STATUS_CHANGED: c.JourneySummary,
    K.JOURNEY_REMINDER_DUE: c.JourneyReminderDue,
    K.JOURNEY_LIKED: c.JourneyEngagement,
    K.JOURNEY_COMMENTED: c.JourneyEngagement,
    K.SOCIAL_USER_BLOCKED: c.UserBlocked,
    K.SOCIAL_USER_UNBLOCKED: c.UserBlocked,
    K.SAFETY_CONTENT_HIDDEN: c.ContentModeration,
    K.SAFETY_CONTENT_UNHIDDEN: c.ContentModeration,
}

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
(out / "_envelope.json").write_text(json.dumps(EventEnvelope.model_json_schema(), indent=2, sort_keys=True) + "\n")
for event_type, model in EVENTS.items():
    schema = {"title": f"{event_type} v1 (data)", **model.model_json_schema()}
    (out / f"{event_type}.v1.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
print(f"wrote {len(EVENTS) + 1} schemas to {out}")
