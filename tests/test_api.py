from unittest.mock import AsyncMock, patch

from lipservice.routes import proxy


class TestAuth:
    def test_create_token(self, client):
        resp = client.post("/api/auth/token", json={
            "username": "admin", "password": "changeme",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"].startswith("ls_")
        assert "expires_at" in data

    def test_bad_credentials(self, client):
        resp = client.post("/api/auth/token", json={
            "username": "admin", "password": "wrong",
        })
        assert resp.status_code == 401

    def test_revoke_token(self, client):
        resp = client.post("/api/auth/token", json={
            "username": "admin", "password": "changeme",
        })
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert client.delete("/api/auth/token", headers=headers).status_code == 204
        assert client.get("/api/status", headers=headers).status_code == 401

    def test_missing_token(self, client):
        assert client.get("/api/status").status_code == 401

    def test_invalid_token(self, client):
        headers = {"Authorization": "Bearer ls_bogus"}
        assert client.get("/api/status", headers=headers).status_code == 401


class TestNetworks:
    def test_create(self, client, auth_headers):
        resp = client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com",
            "port": 6697, "tls": True, "nick": "bot",
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "testnet"
        assert data["state"] == "disconnected"
        assert data["channels"] == []

    def test_create_duplicate(self, client, auth_headers):
        body = {"name": "dup", "host": "irc.example.com", "nick": "bot"}
        client.post("/api/networks", json=body, headers=auth_headers)
        assert client.post("/api/networks", json=body, headers=auth_headers).status_code == 409

    def test_list(self, client, auth_headers):
        for name in ("net1", "net2"):
            client.post("/api/networks", json={
                "name": name, "host": "irc.example.com", "nick": "bot",
            }, headers=auth_headers)
        resp = client.get("/api/networks", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_get(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        resp = client.get("/api/networks/testnet", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "testnet"

    def test_get_not_found(self, client, auth_headers):
        assert client.get("/api/networks/nope", headers=auth_headers).status_code == 404

    def test_update(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        resp = client.patch("/api/networks/testnet", json={
            "nick": "newbot",
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["nick"] == "newbot"

    def test_update_disconnects_on_param_change(self, client, auth_headers, mock_network):
        resp = client.patch("/api/networks/testnet", json={
            "host": "irc.other.com",
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["state"] == "disconnected"
        assert resp.json()["host"] == "irc.other.com"
        mock_network.disconnect.assert_called_once()

    def test_delete(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        assert client.delete("/api/networks/testnet", headers=auth_headers).status_code == 204
        assert client.get("/api/networks/testnet", headers=auth_headers).status_code == 404

    def test_connect(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        with patch("lipservice.routes.IRCClient") as MockIRC:
            mock = AsyncMock()
            mock.connected = True
            MockIRC.return_value = mock

            resp = client.post("/api/networks/testnet/connect", headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json()["state"] == "connecting"
            mock.connect.assert_called_once()

            net = proxy.networks["testnet"]
            assert net.auto_reconnect is True

    def test_connect_idempotent(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "testnet", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        with patch("lipservice.routes.IRCClient") as MockIRC:
            mock = AsyncMock()
            mock.connected = True
            MockIRC.return_value = mock

            client.post("/api/networks/testnet/connect", headers=auth_headers)
            client.post("/api/networks/testnet/connect", headers=auth_headers)
            assert MockIRC.call_count == 1

    def test_disconnect(self, client, auth_headers, mock_network):
        net = proxy.networks["testnet"]
        net.auto_reconnect = True
        resp = client.post("/api/networks/testnet/disconnect", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["state"] == "disconnected"
        mock_network.disconnect.assert_called_once()
        assert net.auto_reconnect is False


class TestChannels:
    def test_list(self, client, auth_headers, mock_network):
        resp = client.get("/api/networks/testnet/channels", headers=auth_headers)
        assert resp.status_code == 200
        channels = resp.json()
        assert len(channels) == 1
        assert channels[0]["name"] == "#test"
        assert channels[0]["topic"] == "Test topic"
        assert channels[0]["members_count"] == 2

    def test_get(self, client, auth_headers, mock_network):
        resp = client.get("/api/networks/testnet/channels/%23test", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "#test"
        assert resp.json()["topic_set_by"] == "someone"

    def test_get_not_found(self, client, auth_headers, mock_network):
        assert client.get(
            "/api/networks/testnet/channels/%23nope", headers=auth_headers,
        ).status_code == 404

    def test_join(self, client, auth_headers, mock_network):
        resp = client.post("/api/networks/testnet/channels", json={
            "name": "#new",
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["name"] == "#new"
        mock_network.join.assert_called_once_with("#new", None)

    def test_join_already_joined(self, client, auth_headers, mock_network):
        resp = client.post("/api/networks/testnet/channels", json={
            "name": "#test",
        }, headers=auth_headers)
        assert resp.status_code == 409

    def test_part(self, client, auth_headers, mock_network):
        assert client.delete(
            "/api/networks/testnet/channels/%23test", headers=auth_headers,
        ).status_code == 204
        mock_network.part.assert_called_once_with("#test")

    def test_set_topic(self, client, auth_headers, mock_network):
        resp = client.put(
            "/api/networks/testnet/channels/%23test/topic",
            json={"text": "New topic"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        mock_network.set_topic.assert_called_once_with("#test", "New topic")


class TestMembers:
    def test_list(self, client, auth_headers, mock_network):
        resp = client.get(
            "/api/networks/testnet/channels/%23test/members", headers=auth_headers,
        )
        assert resp.status_code == 200
        nicks = {m["nick"] for m in resp.json()}
        assert nicks == {"testbot", "alice"}

    def test_not_found(self, client, auth_headers, mock_network):
        assert client.get(
            "/api/networks/testnet/channels/%23nope/members", headers=auth_headers,
        ).status_code == 404


class TestMessages:
    def test_list_channel_messages(self, client, auth_headers, mock_network):
        resp = client.get(
            "/api/networks/testnet/channels/%23test/messages", headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["from"] == "alice"
        assert data["messages"][1]["text"] == "hi alice"

    def test_list_with_limit(self, client, auth_headers, mock_network):
        resp = client.get(
            "/api/networks/testnet/channels/%23test/messages?limit=1",
            headers=auth_headers,
        )
        data = resp.json()
        assert len(data["messages"]) == 1
        assert data["has_more"] is True

    def test_send_channel_message(self, client, auth_headers, mock_network):
        resp = client.post(
            "/api/networks/testnet/channels/%23test/messages",
            json={"text": "hello"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["text"] == "hello"
        assert resp.json()["from"] == "testbot"
        mock_network.privmsg.assert_called_once_with("#test", "hello")

    def test_send_notice(self, client, auth_headers, mock_network):
        client.post(
            "/api/networks/testnet/channels/%23test/messages",
            json={"text": "a notice", "type": "notice"},
            headers=auth_headers,
        )
        mock_network.notice.assert_called_once_with("#test", "a notice")

    def test_send_action(self, client, auth_headers, mock_network):
        client.post(
            "/api/networks/testnet/channels/%23test/messages",
            json={"text": "waves", "type": "action"},
            headers=auth_headers,
        )
        mock_network.privmsg.assert_called_once_with("#test", "\x01ACTION waves\x01")

    def test_send_private_message(self, client, auth_headers, mock_network):
        resp = client.post(
            "/api/networks/testnet/messages/alice",
            json={"text": "hi"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["from"] == "testbot"
        mock_network.privmsg.assert_called_once_with("alice", "hi")

    def test_list_private_messages_empty(self, client, auth_headers, mock_network):
        resp = client.get("/api/networks/testnet/messages/bob", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_send_then_list_private(self, client, auth_headers, mock_network):
        for text in ("first", "second"):
            client.post(
                "/api/networks/testnet/messages/alice",
                json={"text": text},
                headers=auth_headers,
            )
        resp = client.get("/api/networks/testnet/messages/alice", headers=auth_headers)
        data = resp.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["text"] == "first"


class TestUser:
    def test_get_user(self, client, auth_headers):
        assert client.get("/api/user", headers=auth_headers).json()["username"] == "admin"

    def test_get_user_networks(self, client, auth_headers, mock_network):
        data = client.get("/api/user", headers=auth_headers).json()
        assert "testnet" in data["networks"]

    def test_get_network_user(self, client, auth_headers, mock_network):
        resp = client.get("/api/networks/testnet/user", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["nick"] == "testbot"

    def test_change_nick(self, client, auth_headers, mock_network):
        resp = client.put("/api/networks/testnet/user/nick", json={
            "nick": "newnick",
        }, headers=auth_headers)
        assert resp.status_code == 200
        mock_network.set_nick.assert_called_once_with("newnick")


class TestRaw:
    def test_send_raw(self, client, auth_headers, mock_network):
        resp = client.post("/api/networks/testnet/raw", json={
            "command": "WHOIS alice",
        }, headers=auth_headers)
        assert resp.status_code == 202
        mock_network.send_raw.assert_called_once_with("WHOIS alice")

    def test_send_raw_not_connected(self, client, auth_headers):
        client.post("/api/networks", json={
            "name": "offline", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        resp = client.post("/api/networks/offline/raw", json={
            "command": "WHOIS alice",
        }, headers=auth_headers)
        assert resp.status_code == 502


class TestStatus:
    def test_status(self, client, auth_headers):
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "0.2.0"
        assert "uptime_seconds" in data
        assert data["networks_connected"] == 0

    def test_status_counts(self, client, auth_headers, mock_network):
        client.post("/api/networks", json={
            "name": "offline", "host": "irc.example.com", "nick": "bot",
        }, headers=auth_headers)
        resp = client.get("/api/status", headers=auth_headers)
        data = resp.json()
        assert data["networks_connected"] == 1
        assert data["networks_disconnected"] == 1
