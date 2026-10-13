from rest_framework import status
from rest_framework.exceptions import ValidationError, NotFound, APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler


def _flatten(value, prefix=""):
    parts = []
    if isinstance(value, dict):
        for key, val in value.items():
            parts.extend(_flatten(val, f"{prefix}{key}: "))
    elif isinstance(value, (list, tuple)):
        for val in value:
            parts.extend(_flatten(val, prefix))
    else:
        parts.append(f"{prefix}{value}")
    return parts


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        # Never leak SQL, environment values, or tracebacks in API responses.
        return Response({"error": {"code": "SERVER_ERROR", "message": "An unexpected server error occurred.", "details": []}}, status=500)

    status_code = response.status_code
    if isinstance(exc, ValidationError) or status_code == status.HTTP_400_BAD_REQUEST:
        code, message = "VALIDATION_ERROR", "The request contains invalid or missing fields."
        details = _flatten(response.data)
    elif isinstance(exc, NotFound) or status_code == status.HTTP_404_NOT_FOUND:
        code, message, details = "NOT_FOUND", "The requested resource was not found.", []
    elif status_code == status.HTTP_409_CONFLICT:
        code = getattr(exc, "error_code", "INVALID_TRANSITION")
        message, details = str(getattr(exc, "detail", "The request conflicts with the current state.")), []
    elif status_code == status.HTTP_502_BAD_GATEWAY:
        code = getattr(exc, "error_code", "SUPPLIER_UNAVAILABLE")
        message, details = str(getattr(exc, "detail", "An upstream service is unavailable.")), []
    else:
        code, message, details = "REQUEST_ERROR", "The request could not be completed.", _flatten(response.data)

    return Response({"error": {"code": code, "message": message, "details": details}}, status=status_code)


class ContractAPIException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The request is invalid."
    default_code = "VALIDATION_ERROR"

    def __init__(self, code, message, status_code=400, details=None):
        self.status_code = status_code
        self.error_code = code
        self.details = details or []
        super().__init__(detail=message, code=code)
