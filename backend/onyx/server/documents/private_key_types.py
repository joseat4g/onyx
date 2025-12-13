import base64
import json
from enum import Enum
from typing import Protocol

from fastapi import HTTPException
from fastapi import UploadFile

from onyx.server.documents.document_utils import validate_pkcs12_content


class ProcessPrivateKeyFileProtocol(Protocol):
    def __call__(self, file: UploadFile) -> str:
        """
        Accepts a file-like object, validates the file (e.g., checks extension and content),
        and returns its contents as a base64-encoded string if valid.
        Raises an exception if validation fails.
        """
        ...


class PrivateKeyFileTypes(Enum):
    SHAREPOINT_PFX_FILE = "sharepoint_pfx_file"
    BOX_JSON_CONFIG_FILE = "box_json_config_file"


def process_sharepoint_private_key_file(file: UploadFile) -> str:
    """
    Process and validate a private key file upload.

    Validates both the file extension and file content to ensure it's a valid PKCS#12 file.
    Content validation prevents attacks that rely on file extension spoofing.
    """
    # First check file extension (basic filter)
    if not (file.filename and file.filename.lower().endswith(".pfx")):
        raise HTTPException(
            status_code=400, detail="Invalid file type. Only .pfx files are supported."
        )

    # Read file content for validation and processing
    private_key_bytes = file.file.read()

    # Validate file content to prevent extension spoofing attacks
    if not validate_pkcs12_content(private_key_bytes):
        raise HTTPException(
            status_code=400,
            detail="Invalid file content. The uploaded file does not appear to be a valid PKCS#12 (.pfx) file.",
        )

    # Convert to base64 if validation passes
    pfx_64 = base64.b64encode(private_key_bytes).decode("ascii")
    return pfx_64


def process_box_json_config_file(file: UploadFile) -> str:
    """
    Process and validate a Box JSON configuration file upload.

    This validates that the file is a valid JSON file and contains the required
    Box JWT authentication fields.
    """
    # First check file extension (basic filter)
    if not (file.filename and file.filename.lower().endswith(".json")):
        raise HTTPException(
            status_code=400, detail="Invalid file type. Only .json files are supported."
        )

    # Read file content
    file_bytes = file.file.read()

    # Validate JSON content
    try:
        json_config = json.loads(file_bytes.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON file. Please ensure you uploaded a valid Box configuration file: {str(e)}",
        )

    # Validate required Box JWT fields
    required_fields = ["boxAppSettings", "enterpriseID"]
    for field in required_fields:
        if field not in json_config:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Box configuration file. Missing required field: {field}. "
                "Please download the configuration file from your Box App's configuration page.",
            )

    # Validate boxAppSettings structure
    if "boxAppSettings" in json_config:
        box_app_settings = json_config["boxAppSettings"]
        required_app_settings = ["clientID", "clientSecret", "appAuth"]

        for field in required_app_settings:
            if field not in box_app_settings:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid Box configuration file. Missing required field in boxAppSettings: {field}",
                )

        # Validate appAuth structure
        if "appAuth" in box_app_settings:
            app_auth = box_app_settings["appAuth"]
            required_auth_fields = ["publicKeyID", "privateKey"]

            for field in required_auth_fields:
                if field not in app_auth:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid Box configuration file. Missing required field in appAuth: {field}",
                    )

    # Convert to base64 if validation passes
    config_base64 = base64.b64encode(file_bytes).decode("ascii")
    return config_base64


FILE_TYPE_TO_FILE_PROCESSOR: dict[
    PrivateKeyFileTypes, ProcessPrivateKeyFileProtocol
] = {
    PrivateKeyFileTypes.SHAREPOINT_PFX_FILE: process_sharepoint_private_key_file,
    PrivateKeyFileTypes.BOX_JSON_CONFIG_FILE: process_box_json_config_file,
}
