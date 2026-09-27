import fakeredis
from django_redis.client import DefaultClient

server = fakeredis.FakeServer()


class FakeRedis(fakeredis.FakeStrictRedis):
    def info(self, *args, **kwargs):
        return {"redis_version": "8.2.0"}


class FakeRedisClient(DefaultClient):
    def connect(self, index=0):
        return FakeRedis(server=server)
