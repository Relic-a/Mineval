"""Optional VPS transport for a host-side batch worker."""


def client(host):
    from .remote import SSHClient
    return SSHClient(host)


def dispatch(workflow, stop_event, job_id, refresh_export):
    from .remote_dispatch import dispatch as work
    return work(workflow, stop_event, job_id, refresh_export)


def pending_grade(store, job):
    from .remote_dispatch import pending_grade as inspect
    return inspect(store, job)
