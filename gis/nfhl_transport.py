"""Verified TLS 1.2 and HTTP/1.1 for FEMA, normal verified TLS for mirrors."""
import ssl
import requests
from requests.adapters import HTTPAdapter
from arcgis import ArcGIS

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json,text/html,application/zip,*/*",
}


class TLS12Adapter(HTTPAdapter):
    def __init__(self, **kwargs):
        self.context = ssl.create_default_context()
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.maximum_version = ssl.TLSVersion.TLSv1_2
        # Keep Python/OpenSSL's default cipher policy and certificate validation.
        self.context.set_alpn_protocols(["http/1.1"])
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self.context
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        kwargs["ssl_context"] = self.context
        return host, kwargs

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self.context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def client(output):
    session = requests.Session()  # requests/urllib3 uses HTTP/1.1, not HTTP/2.
    session.mount("https://hazards.fema.gov/", TLS12Adapter())
    session.mount("https://msc.fema.gov/", TLS12Adapter())
    return ArcGIS(output, session=session, timeout=60, headers=BROWSER)
