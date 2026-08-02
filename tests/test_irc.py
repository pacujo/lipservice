import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock

from lipservice.irc import IRCClient, IRCMessage, format_irc, parse_irc


class TestParseIRC:
    def test_ping(self):
        msg = parse_irc("PING :server.example.com")
        assert msg.command == "PING"
        assert msg.params == ["server.example.com"]

    def test_privmsg(self):
        msg = parse_irc(":nick!user@host PRIVMSG #channel :Hello world")
        assert msg.prefix == "nick!user@host"
        assert msg.command == "PRIVMSG"
        assert msg.params == ["#channel", "Hello world"]

    def test_numeric_welcome(self):
        msg = parse_irc(":server 001 nick :Welcome to the network")
        assert msg.prefix == "server"
        assert msg.command == "001"
        assert msg.params == ["nick", "Welcome to the network"]

    def test_join_no_trailing(self):
        msg = parse_irc(":nick!user@host JOIN #channel")
        assert msg.command == "JOIN"
        assert msg.params == ["#channel"]
        assert msg.prefix == "nick!user@host"

    def test_tags(self):
        msg = parse_irc("@time=2026-03-14T10:00:00Z :nick PRIVMSG #ch :hi")
        assert msg.tags == {"time": "2026-03-14T10:00:00Z"}
        assert msg.command == "PRIVMSG"
        assert msg.params == ["#ch", "hi"]

    def test_multiple_tags(self):
        msg = parse_irc("@time=123;batch=abc :nick PRIVMSG #ch :hi")
        assert msg.tags == {"time": "123", "batch": "abc"}

    def test_tag_without_value(self):
        msg = parse_irc("@draft/flag :nick PRIVMSG #ch :hi")
        assert msg.tags == {"draft/flag": ""}

    def test_colon_in_trailing(self):
        msg = parse_irc(":nick PRIVMSG #ch :hello : world")
        assert msg.params == ["#ch", "hello : world"]

    def test_names_reply(self):
        msg = parse_irc(":server 353 nick = #channel :@alice +bob carol")
        assert msg.command == "353"
        assert msg.params == ["nick", "=", "#channel", "@alice +bob carol"]

    def test_quit_with_message(self):
        msg = parse_irc(":nick!user@host QUIT :Gone away")
        assert msg.command == "QUIT"
        assert msg.params == ["Gone away"]

    def test_mode(self):
        msg = parse_irc(":nick MODE #channel +o alice")
        assert msg.command == "MODE"
        assert msg.params == ["#channel", "+o", "alice"]

    def test_kick(self):
        msg = parse_irc(":op!user@host KICK #channel target :reason here")
        assert msg.command == "KICK"
        assert msg.params == ["#channel", "target", "reason here"]

    def test_no_prefix_no_trailing(self):
        msg = parse_irc("NICK newnick")
        assert msg.prefix is None
        assert msg.command == "NICK"
        assert msg.params == ["newnick"]

    def test_action(self):
        msg = parse_irc(":nick!user@host PRIVMSG #ch :\x01ACTION waves\x01")
        assert msg.params == ["#ch", "\x01ACTION waves\x01"]

    def test_case_normalization(self):
        msg = parse_irc("ping :server")
        assert msg.command == "PING"


class TestFormatIRC:
    def test_no_params(self):
        assert format_irc("QUIT") == "QUIT"

    def test_simple_param(self):
        assert format_irc("NICK", "alice") == "NICK alice"

    def test_trailing_with_space(self):
        assert format_irc("PRIVMSG", "#test", "hello world") == "PRIVMSG #test :hello world"

    def test_trailing_without_space(self):
        assert format_irc("JOIN", "#test") == "JOIN #test"

    def test_multiple_params_trailing(self):
        assert format_irc("USER", "alice", "0", "*", "Real Name") == "USER alice 0 * :Real Name"

    def test_trailing_starts_with_colon(self):
        assert format_irc("PONG", ":server") == "PONG ::server"

    def test_roundtrip_privmsg(self):
        original = ":nick PRIVMSG #test :hello world"
        msg = parse_irc(original)
        rebuilt = format_irc(msg.command, *msg.params)
        reparsed = parse_irc(rebuilt)
        assert reparsed.command == msg.command
        assert reparsed.params == msg.params


class TestIRCClientReadLoop:
    def test_read_loop_ssl_close_notify(self) -> None:
        events: list[tuple[str, str, dict[str, object]]] = []

        async def on_event(
            network: str, kind: str, data: dict[str, object],
        ) -> None:
            events.append((network, kind, data))

        client = IRCClient(
            "testnet", "irc.example.com", 6697, True,
            "nick", "user", None, on_event,
        )
        client.connected = True
        client.registered = True

        reader = MagicMock()
        reader.readline = AsyncMock(
            side_effect=ssl.SSLError(
                1, "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] "
                "application data after close notify",
            ),
        )
        client._reader = reader

        asyncio.run(client._read_loop())

        assert not client.connected
        assert not client.registered
        assert events == [("testnet", "network_state", {"state": "disconnected"})]
