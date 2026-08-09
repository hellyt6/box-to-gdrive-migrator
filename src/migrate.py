"""
box-to-gdrive-migrator

Migrates files from Box to Google Drive by resolving user identity through
Okta rather than matching on email address. See README.md for the full
rationale.

Designed for use as a scheduled AWS Lambda entry point (see
`lambda_handler`), but every function here also runs standalone for local
testing: `python migrate.py --dry-run`.
"""

import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass

import boto3
from boxsdk import Client as BoxClient
from boxsdk import JWTAuth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from okta.client import Client as OktaClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate")

DYNAMO_TABLE_NAME = os.environ.get("MANIFEST_TABLE", "box-gdrive-migration-manifest")
GOOGLE_DELEGATED_SCOPES = ["https://www.googleapis.com/auth/drive"]


@dataclass
class UserMapping:
    """A resolved identity pair pulled from Okta custom profile attributes."""
    okta_id: str
    display_name: str
    box_user_id: str
    google_workspace_id: str


def get_okta_user_mappings(okta_client: OktaClient) -> list[UserMapping]:
    """
    Pull every active Okta user and resolve their Box + Google Workspace
    identities from custom profile attributes.

    Users missing either attribute are skipped and logged, not guessed at —
    silently falling back to email matching here would defeat the entire
    point of this approach.
    """
    mappings = []
    users, resp, err = okta_client.list_users()
    if err:
        raise RuntimeError(f"Okta user list failed: {err}")

    for user in users:
        profile = user.profile
        box_id = getattr(profile, "boxUserId", None)
        gw_id = getattr(profile, "googleWorkspaceId", None)

        if not box_id or not gw_id:
            logger.warning(
                "Skipping %s — missing boxUserId or googleWorkspaceId in Okta profile",
                getattr(profile, "email", user.id),
            )
            continue

        mappings.append(
            UserMapping(
                okta_id=user.id,
                display_name=getattr(profile, "email", user.id),
                box_user_id=box_id,
                google_workspace_id=gw_id,
            )
        )

    logger.info("Resolved %d Okta users with complete Box/Google mappings", len(mappings))
    return mappings


def get_box_client_as_user(admin_client: BoxClient, box_user_id: str) -> BoxClient:
    """Impersonate a Box user via admin JWT auth to list their files."""
    return admin_client.as_user(admin_client.user(user_id=box_user_id))


def list_box_files(user_box_client: BoxClient, folder_id: str = "0") -> list[dict]:
    """
    Recursively list files in a user's Box account starting from the given
    folder (root = "0"). Returns flat list of file metadata dicts.
    """
    files = []
    items = user_box_client.folder(folder_id=folder_id).get_items()
    for item in items:
        if item.type == "file":
            files.append({"id": item.id, "name": item.name, "path_folder": folder_id})
        elif item.type == "folder":
            files.extend(list_box_files(user_box_client, item.id))
    return files


def get_google_drive_service(google_workspace_id: str, service_account_path: str):
    """
    Build a Drive service impersonating the target Workspace user via
    domain-wide delegation. The service account itself never has direct
    access to user data — the delegation is scoped per-call to the
    specific destination user.
    """
    credentials = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=GOOGLE_DELEGATED_SCOPES
    )
    delegated_credentials = credentials.with_subject(google_workspace_id)
    return build("drive", "v3", credentials=delegated_credentials)


def already_migrated(dynamo_table, box_file_id: str, checksum: str) -> bool:
    """Idempotency check against the migration manifest table."""
    response = dynamo_table.get_item(Key={"box_file_id": box_file_id})
    item = response.get("Item")
    return bool(item and item.get("checksum") == checksum)


def record_migration(dynamo_table, box_file_id: str, checksum: str, drive_file_id: str) -> None:
    dynamo_table.put_item(
        Item={
            "box_file_id": box_file_id,
            "checksum": checksum,
            "drive_file_id": drive_file_id,
        }
    )


def checksum_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def migrate_user_files(
    mapping: UserMapping,
    box_admin_client: BoxClient,
    service_account_path: str,
    dynamo_table,
    dry_run: bool = False,
) -> dict:
    """Migrate all of one user's Box files into their matched Drive account."""
    user_box_client = get_box_client_as_user(box_admin_client, mapping.box_user_id)
    files = list_box_files(user_box_client)

    drive_service = None if dry_run else get_google_drive_service(
        mapping.google_workspace_id, service_account_path
    )

    migrated, skipped = 0, 0
    for f in files:
        box_file = user_box_client.file(f["id"]).get()
        content = box_file.content()
        checksum = checksum_bytes(content)

        if already_migrated(dynamo_table, f["id"], checksum):
            skipped += 1
            continue

        if dry_run:
            logger.info("[DRY RUN] Would migrate %s (%s) for %s", f["name"], f["id"], mapping.display_name)
            migrated += 1
            continue

        media_body = _bytes_to_media_upload(content, f["name"])
        drive_file = drive_service.files().create(
            body={"name": f["name"]}, media_body=media_body, fields="id"
        ).execute()

        record_migration(dynamo_table, f["id"], checksum, drive_file["id"])
        migrated += 1

    logger.info(
        "%s: migrated %d, skipped %d (already migrated)",
        mapping.display_name, migrated, skipped,
    )
    return {"user": mapping.display_name, "migrated": migrated, "skipped": skipped}


def _bytes_to_media_upload(content: bytes, filename: str):
    from io import BytesIO

    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(BytesIO(content), mimetype="application/octet-stream", resumable=True)


def run_migration(dry_run: bool = False) -> list[dict]:
    okta_client = OktaClient(
        {"orgUrl": os.environ["OKTA_ORG_URL"], "token": os.environ["OKTA_API_TOKEN"]}
    )
    box_auth = JWTAuth.from_settings_file(os.environ["BOX_JWT_CONFIG_PATH"])
    box_admin_client = BoxClient(box_auth)

    dynamodb = boto3.resource("dynamodb")
    dynamo_table = dynamodb.Table(DYNAMO_TABLE_NAME)

    mappings = get_okta_user_mappings(okta_client)
    results = []
    for mapping in mappings:
        results.append(
            migrate_user_files(
                mapping,
                box_admin_client,
                os.environ["GOOGLE_SERVICE_ACCOUNT_PATH"],
                dynamo_table,
                dry_run=dry_run,
            )
        )
    return results


def lambda_handler(event, context):
    """Entry point for the scheduled AWS Lambda deployment (see terraform/)."""
    results = run_migration(dry_run=False)
    return {"statusCode": 200, "body": json.dumps(results)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Box to Google Drive identity-aware migration")
    parser.add_argument("--dry-run", action="store_true", help="List what would migrate without transferring")
    args = parser.parse_args()

    results = run_migration(dry_run=args.dry_run)
    print(json.dumps(results, indent=2))
