"""PostgreSQL connection helpers for cluster admin configuration data."""
import os

STORAGE_VIP = "10.0.0.100"
DATABASE = "ycluster"
ROLE = "ycluster"
PASSWORD_KEY = "/cluster/config/ycluster/db-password"


def connect():
    """Connect with the dedicated least-privilege admin config role."""
    import psycopg2

    from ycluster.common.etcd_utils import get_etcd_client
    password = get_etcd_client().get(PASSWORD_KEY)[0]
    if password is None:
        raise RuntimeError("admin database password not found in etcd")
    host = os.environ.get("ADMIN_DB_HOST", STORAGE_VIP)
    return psycopg2.connect(host=host, database=DATABASE, user=ROLE,
                            password=password.decode()
                            if isinstance(password, bytes) else password)
