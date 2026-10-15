"""验收共用禁网守卫：仅放行数字回环地址，供 Windows 事件循环自唤醒。"""
from ipaddress import ip_address
import socket


def require_loopback(address):
    # 不解析主机名，避免守卫本身发起 DNS；localhost 也必须使用数字地址。
    try:
        host = address[0] if isinstance(address, tuple) else None
        allowed = isinstance(host, str) and ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise AssertionError(f"验收禁止非回环网络连接：{address!r}")


def install_network_guard(patch=setattr):
    """patch 可传 monkeypatch.setattr；插件进程则使用默认 setattr。"""
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    create_connection = socket.create_connection

    def guarded_connect(self, address):
        require_loopback(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        require_loopback(address)
        return connect_ex(self, address)

    def guarded_create(address, *args, **kwargs):
        require_loopback(address)
        return create_connection(address, *args, **kwargs)

    def forbidden_datagram(*args, **kwargs):
        raise AssertionError("验收禁止数据报出站")

    patch(socket.socket, "connect", guarded_connect)
    patch(socket.socket, "connect_ex", guarded_connect_ex)
    patch(socket, "create_connection", guarded_create)
    patch(socket.socket, "sendto", forbidden_datagram)
    if hasattr(socket.socket, "sendmsg"):
        patch(socket.socket, "sendmsg", forbidden_datagram)
