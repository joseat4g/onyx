import base64
import json
from datetime import datetime
from datetime import timezone
from io import BytesIO
from typing import Any

from boxsdk import Client
from boxsdk.auth.jwt_auth import JWTAuth
from boxsdk.auth.oauth2 import OAuth2
from boxsdk.exception import BoxAPIException
from boxsdk.object.file import File
from boxsdk.object.folder import Folder

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.connectors.exceptions import CredentialInvalidError
from onyx.connectors.exceptions import InsufficientPermissionsError
from onyx.connectors.interfaces import GenerateDocumentsOutput
from onyx.connectors.interfaces import GenerateSlimDocumentOutput
from onyx.connectors.interfaces import LoadConnector
from onyx.connectors.interfaces import PollConnector
from onyx.connectors.interfaces import SecondsSinceUnixEpoch
from onyx.connectors.interfaces import SlimConnector
from onyx.connectors.models import ConnectorMissingCredentialError
from onyx.connectors.models import Document
from onyx.connectors.models import SlimDocument
from onyx.connectors.models import TextSection
from onyx.file_processing.extract_file_text import extract_file_text
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.utils.logger import setup_logger


logger = setup_logger()


class BoxConnector(LoadConnector, PollConnector, SlimConnector):
    IMAGE_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
        ".svg",
        ".webp",
        ".ico",
    }

    def __init__(self, batch_size: int = INDEX_BATCH_SIZE) -> None:
        self.batch_size = batch_size
        self.box_client: Client | None = None
        self.allow_images = False

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load Box credentials from the provided dictionary.
        Supports two authentication methods:
        1. OAuth2: requires client_id, client_secret, access_token (and optionally refresh_token)
        2. JWT: requires box_json_config (base64-encoded JSON configuration file)
        """
        try:
            # Check if JWT authentication is being used
            if "box_json_config" in credentials and credentials["box_json_config"]:
                # Decode the base64-encoded JSON config
                try:
                    json_config_base64 = credentials["box_json_config"]
                    json_config_str = base64.b64decode(json_config_base64).decode(
                        "utf-8"
                    )
                    json_config = json.loads(json_config_str)
                except Exception as e:
                    logger.error(f"Error decoding Box JSON config: {str(e)}")
                    raise CredentialInvalidError(
                        f"Invalid Box JSON configuration file. Please ensure you uploaded the correct file from Box: {str(e)}"
                    )

                # Create JWT auth from the config
                try:
                    auth = JWTAuth.from_settings_dictionary(json_config)
                    # Authenticate to get the service account client
                    auth.authenticate_instance()
                    self.box_client = Client(auth)
                    logger.info("Box JWT authentication successful")
                    return None
                except Exception as e:
                    logger.error(f"Error creating Box JWT client: {str(e)}")
                    raise CredentialInvalidError(
                        f"Failed to authenticate with Box JWT. Please verify your JSON config "
                        f"is correct and the app is authorized: {str(e)}"
                    )

            # Otherwise, use OAuth2 authentication (existing method)
            else:
                oauth = OAuth2(
                    client_id=credentials["client_id"],
                    client_secret=credentials["client_secret"],
                    access_token=credentials["access_token"],
                    refresh_token=credentials.get("refresh_token"),
                )

                # Create the Box client
                self.box_client = Client(oauth)

                # Check if we need to refresh the token and update it
                if credentials.get("refresh_token"):
                    access_token, refresh_token = oauth.refresh(oauth.access_token)
                    credentials["access_token"] = access_token
                    credentials["refresh_token"] = refresh_token
                    return credentials

                return None

        except CredentialInvalidError:
            raise
        except Exception as e:
            logger.error(f"Error creating Box client: {str(e)}")
            raise CredentialInvalidError(f"Failed to initialize Box client: {str(e)}")

    def set_allow_images(self, value: bool) -> None:
        self.allow_images = value

    def _download_file(self, file: File) -> bytes:
        """Download a single file from Box."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        try:
            file_content = BytesIO()
            file.download_to(file_content)
            file_content.seek(0)
            return file_content.read()
        except BoxAPIException as e:
            logger.error(f"Error downloading file {file.id}: {str(e)}")
            raise

    def _process_file(
        self,
        file: File,
        start: SecondsSinceUnixEpoch | None = None,
        end: SecondsSinceUnixEpoch | None = None,
    ) -> Document | None:
        """Process a Box file and convert it to a Document."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        try:
            # Get file information
            file_info = file.get()
            file_id = file_info.id
            file_name = file_info.name

            # Convert modified time to UTC
            modified_at_str = file_info.modified_at
            if modified_at_str is None:
                modified_at = datetime.now(timezone.utc)
            else:
                # Parse ISO 8601 timestamp
                modified_at = datetime.fromisoformat(
                    modified_at_str.replace("Z", "+00:00")
                )
                if modified_at.tzinfo is None:
                    modified_at = modified_at.replace(tzinfo=timezone.utc)
                else:
                    modified_at = modified_at.astimezone(timezone.utc)

            time_as_seconds = int(modified_at.timestamp())
            if start and time_as_seconds < start:
                return None
            if end and time_as_seconds > end:
                return None

            # Skip certain file types like executables, videos, etc.
            if self._should_skip_file(file_name):
                logger.info(f"Skipping file {file_name} due to file type")
                return None

            # Skip image files if image processing is disabled
            if not self.allow_images and self._is_image_file(file_name):
                logger.info(
                    f"Skipping image file {file_name} as image processing is disabled"
                )
                return None

            # Download and extract text from file
            try:
                file_content = self._download_file(file)
                text = extract_file_text(
                    BytesIO(file_content),
                    file_name=file_name,
                    break_on_unprocessable=False,
                )

                # Create metadata with file information
                metadata = {
                    "type": "file",
                    "file_type": (
                        file_name.split(".")[-1].lower()
                        if "." in file_name
                        else "unknown"
                    ),
                    "owner": (
                        file_info.owned_by.name
                        if hasattr(file_info, "owned_by") and file_info.owned_by
                        else "Unknown"
                    ),
                }

                # Create document
                return Document(
                    id=f"box:{file_id}",
                    sections=[TextSection(text=text, link=file_info.web_url)],
                    source=DocumentSource.BOX,
                    semantic_identifier=file_name,
                    doc_updated_at=modified_at,
                    metadata=metadata,
                )
            except Exception as e:
                logger.exception(
                    f"Error processing file {file_name} (ID: {file_id}): {str(e)}"
                )
                return None
        except BoxAPIException as e:
            logger.exception(f"Box API exception for file {file.id}: {str(e)}")
            return None

    def _should_skip_file(self, file_name: str) -> bool:
        """Check if we should skip indexing this file based on its name/extension."""
        skip_extensions = {
            # Executables
            ".exe",
            ".dll",
            ".bin",
            ".app",
            # Videos
            ".mp4",
            ".avi",
            ".mov",
            ".wmv",
            # Audio
            ".mp3",
            ".wav",
            ".ogg",
            # Archives
            ".zip",
            ".tar",
            ".gz",
            ".rar",
            # Database/system files
            ".db",
            ".sqlite",
            ".dat",
            ".bak",
        }

        if "." in file_name:
            extension = f".{file_name.split('.')[-1].lower()}"
            return extension in skip_extensions
        return False

    def _is_image_file(self, file_name: str) -> bool:
        """Check if the file is an image."""
        if "." in file_name:
            extension = f".{file_name.split('.')[-1].lower()}"
            return extension in self.IMAGE_EXTENSIONS
        return False

    def _yield_files_recursive(
        self,
        folder_id: str,
        start: SecondsSinceUnixEpoch | None = None,
        end: SecondsSinceUnixEpoch | None = None,
    ) -> GenerateDocumentsOutput:
        """Recursively yield files from Box folders."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        try:
            folder = self.box_client.folder(folder_id=folder_id).get()
            items = []
            offset = 0
            limit = 100  # Box API pagination limit

            # Use Box API pagination to get items in the folder
            while True:
                items_page = folder.get_items(limit=limit, offset=offset)
                items_in_page = list(items_page)

                if not items_in_page:
                    break

                items.extend(items_in_page)
                offset += limit

                # Box API returns fewer items than the limit when there are no more items
                if len(items_in_page) < limit:
                    break

            # Process items in batches
            current_batch: list[Document] = []

            for item in items:
                if isinstance(item, File):
                    document = self._process_file(item, start, end)
                    if document:
                        current_batch.append(document)

                        # Yield batch when it reaches the batch size
                        if len(current_batch) >= self.batch_size:
                            yield current_batch
                            current_batch = []

                elif isinstance(item, Folder):
                    # Process subfolders recursively
                    # First yield the current batch if it's not empty
                    if current_batch:
                        yield current_batch
                        current_batch = []

                    # Then process the subfolder
                    yield from self._yield_files_recursive(item.id, start, end)

            # Yield any remaining items in the batch
            if current_batch:
                yield current_batch

        except BoxAPIException as e:
            logger.exception(
                f"Box API exception when listing folder {folder_id}: {str(e)}"
            )
            raise

    def load_from_state(self) -> GenerateDocumentsOutput:
        """Load all documents from Box."""
        return self.poll_source(None, None)

    def poll_source(
        self, start: SecondsSinceUnixEpoch | None, end: SecondsSinceUnixEpoch | None
    ) -> GenerateDocumentsOutput:
        """Poll Box for new/updated documents within a time range."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        # Use '0' as the root folder ID in Box
        for batch in self._yield_files_recursive("0", start, end):
            yield batch

        return None

    def retrieve_all_slim_docs(
        self,
        start: SecondsSinceUnixEpoch | None = None,
        end: SecondsSinceUnixEpoch | None = None,
        callback: IndexingHeartbeatInterface | None = None,
    ) -> GenerateSlimDocumentOutput:
        """Retrieve slim document representations for pruning."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box")

        try:
            limit = 100  # Box API pagination limit
            current_batch: list[SlimDocument] = []

            # Get all items recursively
            def get_items_recursively(folder_id: str) -> None:
                nonlocal current_batch

                folder = self.box_client.folder(folder_id=folder_id).get()
                offset = 0

                while True:
                    items_page = folder.get_items(limit=limit, offset=offset)
                    items_in_page = list(items_page)

                    if not items_in_page:
                        break

                    for item in items_in_page:
                        if isinstance(item, File):
                            # Get file information
                            file_info = item.get()
                            file_id = file_info.id

                            # Convert modified time to UTC
                            modified_at_str = file_info.modified_at
                            if modified_at_str is None:
                                continue

                            modified_at = datetime.fromisoformat(
                                modified_at_str.replace("Z", "+00:00")
                            )
                            if modified_at.tzinfo is None:
                                modified_at = modified_at.replace(tzinfo=timezone.utc)
                            else:
                                modified_at = modified_at.astimezone(timezone.utc)

                            time_as_seconds = int(modified_at.timestamp())
                            if start and time_as_seconds < start:
                                continue
                            if end and time_as_seconds > end:
                                continue

                            # Create slim document
                            current_batch.append(
                                SlimDocument(
                                    id=f"box:{file_id}",
                                    doc_updated_at=modified_at,
                                )
                            )

                            # Yield batch when it reaches the batch size
                            if len(current_batch) >= self.batch_size:
                                if callback:
                                    callback.heartbeat()
                                yield current_batch
                                current_batch = []

                        elif isinstance(item, Folder):
                            # Process subfolder recursively
                            get_items_recursively(item.id)

                    offset += limit

                    # Box API returns fewer items than the limit when there are no more items
                    if len(items_in_page) < limit:
                        break

            # Start recursive retrieval from root folder
            get_items_recursively("0")

            # Yield any remaining items in the batch
            if current_batch:
                if callback:
                    callback.heartbeat()
                yield current_batch

        except BoxAPIException as e:
            logger.exception(
                f"Box API exception during slim document retrieval: {str(e)}"
            )
            raise

    def validate_connector_settings(self) -> None:
        """Validate Box connector settings by checking access to the root folder."""
        if self.box_client is None:
            raise ConnectorMissingCredentialError("Box credentials not loaded.")

        try:
            # Try to access the root folder as a validation check
            self.box_client.folder(folder_id="0").get()
        except BoxAPIException as e:
            logger.exception("Failed to validate Box credentials")

            if e.status == 401:
                raise CredentialInvalidError(
                    f"Box credential is invalid or expired: {str(e)}"
                )
            elif e.status == 403:
                raise InsufficientPermissionsError(
                    "Your Box token does not have sufficient permissions."
                )
            else:
                raise ConnectorValidationError(
                    f"Unexpected Box error during validation: {str(e)}"
                )
        except Exception as e:
            raise Exception(
                f"Unexpected error during Box settings validation: {str(e)}"
            )


if __name__ == "__main__":
    import os

    connector = BoxConnector()
    connector.load_credentials(
        {
            "client_id": os.environ["BOX_CLIENT_ID"],
            "client_secret": os.environ["BOX_CLIENT_SECRET"],
            "access_token": os.environ["BOX_ACCESS_TOKEN"],
            "refresh_token": os.environ.get("BOX_REFRESH_TOKEN"),
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
