class ServiceError(Exception):
    """Base for errors that the API translates to an HTTP status."""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """Conflict with existing state: unique name, object in use, invalid status transition."""


class InvalidReferenceError(ServiceError):
    """A foreign key refers to an object that does not exist."""
