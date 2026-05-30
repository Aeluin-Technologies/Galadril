from pydantic import BaseModel
from typing import List, Optional


class KafkaConnector(BaseModel):
    brokers: List[str]
    schema_registry: str
    consumer_group: str


class S3Connector(BaseModel):
    endpoint: str
    access_key: str
    secret_key: str
    region: str
    bucket_notifications: Optional[str] = None


class PostgresConnector(BaseModel):
    database: str
    host: str
    user: str
    password: str


class SpiceDBConnector(BaseModel):
    endpoint: str
    token: str
    schema_name: Optional[str] = None


class Connectors(BaseModel):
    kafka: Optional[KafkaConnector] = None
    s3: Optional[S3Connector] = None
    postgres: Optional[PostgresConnector] = None
    spicedb: Optional[SpiceDBConnector] = None
