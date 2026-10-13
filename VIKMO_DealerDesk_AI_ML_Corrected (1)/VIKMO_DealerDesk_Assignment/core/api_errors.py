from rest_framework.response import Response


def error_response(code, message, status_code, details=None):
    return Response({"error": {
        "code": code,
        "message": str(message),
        "details": details if isinstance(details, list) else [],
    }}, status=status_code)
