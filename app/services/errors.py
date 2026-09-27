class ServiceError(Exception):
    """Basis voor fouten die de API naar een HTTP-status vertaalt."""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """Botsing met bestaande state: unieke naam, object in gebruik, ongeldige statusovergang."""


class InvalidReferenceError(ServiceError):
    """Een foreign key verwijst naar een object dat niet bestaat."""
