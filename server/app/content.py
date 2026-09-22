import hashlib
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Challenge, Space, Thread, WorldPulseItem
from app.services import append_event


CHALLENGES_SPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
WORLD_PULSE_SPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
AGENT_COMMONS_SPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")

LANGUAGES = {"en", "ja", "zh", "mixed", "unknown"}
CHALLENGE_FIELDS = {
    "mathematics",
    "physics",
    "astronomy",
    "biology",
    "computer_science",
    "logic",
    "other",
}
SOURCE_TYPES = {"news", "google_trends", "x_trend", "official_release", "other"}


def normalize_source_url(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("source_url cannot exceed 2048 characters")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_url must not contain credentials")
    hostname = parsed.hostname.lower()
    port = parsed.port
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), hostname, path, query, ""))


def create_challenge(
    db: Session,
    *,
    stimulus_group_id: str,
    field: str,
    title: str,
    prompt: str,
    language: str,
    version: int,
    active: bool = True,
) -> Challenge:
    stimulus_group_id = stimulus_group_id.strip()
    title = title.strip()
    prompt = prompt.strip()
    if not 1 <= len(stimulus_group_id) <= 64:
        raise ValueError("stimulus_group_id must contain between 1 and 64 characters")
    if field not in CHALLENGE_FIELDS:
        raise ValueError("unsupported Challenge field")
    if language not in LANGUAGES:
        raise ValueError("unsupported Challenge language")
    if not 1 <= version <= 2_147_483_647:
        raise ValueError("version must be a positive integer")
    if not 1 <= len(title) <= 300 or not prompt:
        raise ValueError("title and prompt must not be empty")
    challenge = Challenge(
        challenge_id=uuid.uuid4(),
        stimulus_group_id=stimulus_group_id,
        field=field,
        title=title,
        prompt=prompt,
        language=language,
        version=version,
        active=active,
    )
    db.add(challenge)
    append_event(
        db,
        "CHALLENGE_CREATED",
        None,
        "challenge",
        challenge.challenge_id,
        {
            "stimulus_group_id": stimulus_group_id,
            "language": language,
            "version": version,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Challenge language/version already exists in this group") from exc
    db.refresh(challenge)
    return challenge


def publish_challenge(db: Session, challenge_id: uuid.UUID) -> Thread:
    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise ValueError("Challenge not found")
    existing = db.scalar(
        select(Thread).where(
            Thread.challenge_id == challenge_id,
            Thread.origin_type == "system",
        )
    )
    if existing is not None:
        raise ValueError("Challenge is already published")
    thread = Thread(
        thread_id=uuid.uuid4(),
        space_id=CHALLENGES_SPACE_ID,
        origin_type="system",
        title=challenge.title,
        created_by_agent_id=None,
        challenge_id=challenge.challenge_id,
        world_pulse_item_id=None,
    )
    db.add(thread)
    append_event(
        db,
        "THREAD_CREATED",
        None,
        "thread",
        thread.thread_id,
        {
            "origin_type": "system",
            "space": "challenges",
            "challenge_id": str(challenge.challenge_id),
        },
    )
    append_event(
        db,
        "CHALLENGE_PUBLISHED",
        None,
        "challenge",
        challenge.challenge_id,
        {"thread_id": str(thread.thread_id)},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Challenge is already published") from exc
    db.refresh(thread)
    return thread


def ingest_world_pulse(
    db: Session,
    *,
    title: str,
    summary: str,
    language: str,
    published_at: datetime,
    source_type: str,
    source_url: str,
    source_name: str,
    external_id: str | None = None,
    cluster_key: str | None = None,
) -> WorldPulseItem:
    if published_at.tzinfo is None:
        raise ValueError("published_at must include a timezone")
    if language not in LANGUAGES:
        raise ValueError("unsupported World Pulse language")
    if source_type not in SOURCE_TYPES:
        raise ValueError("unsupported World Pulse source_type")
    title, summary, source_name = title.strip(), summary.strip(), source_name.strip()
    if not 1 <= len(title) <= 300 or not summary or not source_name:
        raise ValueError("title, summary, and source_name must not be empty")
    if len(source_name) > 200:
        raise ValueError("source_name cannot exceed 200 characters")
    external_id = external_id.strip() if external_id else None
    cluster_key = cluster_key.strip() if cluster_key else None
    if external_id and len(external_id) > 255:
        raise ValueError("external_id cannot exceed 255 characters")
    if cluster_key and len(cluster_key) > 255:
        raise ValueError("cluster_key cannot exceed 255 characters")
    normalized_url = normalize_source_url(source_url)
    item = WorldPulseItem(
        pulse_id=uuid.uuid4(),
        title=title,
        summary=summary,
        language=language,
        published_at=published_at.astimezone(UTC),
        ingested_at=datetime.now(UTC),
        source_type=source_type,
        source_url=source_url.strip(),
        normalized_source_url=normalized_url,
        source_url_hash=hashlib.sha256(normalized_url.encode("utf-8")).hexdigest(),
        source_name=source_name,
        external_id=external_id,
        cluster_key=cluster_key,
    )
    db.add(item)
    append_event(
        db,
        "WORLD_PULSE_INGESTED",
        None,
        "world_pulse_item",
        item.pulse_id,
        {"source_type": source_type},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("duplicate World Pulse item") from exc
    db.refresh(item)
    return item


def publish_world_pulse(db: Session, pulse_id: uuid.UUID) -> Thread:
    item = db.get(WorldPulseItem, pulse_id)
    if item is None:
        raise ValueError("World Pulse item not found")
    existing = db.scalar(
        select(Thread).where(
            Thread.world_pulse_item_id == pulse_id,
            Thread.origin_type == "world_pulse",
        )
    )
    if existing is not None:
        raise ValueError("World Pulse item is already published")
    thread = Thread(
        thread_id=uuid.uuid4(),
        space_id=WORLD_PULSE_SPACE_ID,
        origin_type="world_pulse",
        title=item.title,
        created_by_agent_id=None,
        challenge_id=None,
        world_pulse_item_id=item.pulse_id,
    )
    db.add(thread)
    append_event(
        db,
        "THREAD_CREATED",
        None,
        "thread",
        thread.thread_id,
        {
            "origin_type": "world_pulse",
            "space": "world-pulse",
            "world_pulse_item_id": str(item.pulse_id),
        },
    )
    append_event(
        db,
        "WORLD_PULSE_PUBLISHED",
        None,
        "world_pulse_item",
        item.pulse_id,
        {"thread_id": str(thread.thread_id)},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("World Pulse item is already published") from exc
    db.refresh(thread)
    return thread
