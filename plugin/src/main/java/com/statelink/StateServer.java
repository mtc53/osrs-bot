package com.statelink;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import lombok.extern.slf4j.Slf4j;

/**
 * A very small HTTP server, bound to loopback only, that hands the bot the
 * game state the plugin has already collected.
 *
 * Nothing in here touches the game. The snapshot is built on the client
 * thread once per game tick and parked in a volatile string; these handlers
 * only ever read that string. The two endpoints that do need live data
 * (/widget and /watch) hand the work back to the client thread and block on
 * a future, so the game is never read off-thread.
 */
@Slf4j
class StateServer
{
	interface Endpoints
	{
		String snapshot();

		String widget(Map<String, String> query) throws Exception;

		String menu() throws Exception;

		String project(Map<String, String> query) throws Exception;

		String watch(Map<String, String> query);
	}

	private final int port;
	private final String token;
	private final Endpoints endpoints;
	private HttpServer server;
	private ThreadPoolExecutor pool;

	StateServer(int port, String token, Endpoints endpoints)
	{
		this.port = port;
		this.token = token == null ? "" : token.trim();
		this.endpoints = endpoints;
	}

	void start() throws IOException
	{
		// Loopback only. This is a hole in the side of your game client;
		// it does not need to be reachable from the network.
		server = HttpServer.create(
			new InetSocketAddress(InetAddress.getLoopbackAddress(), port), 0);

		server.createContext("/ping", ex -> respond(ex, 200,
			"{\"ok\":true,\"plugin\":\"state-link\"}"));

		server.createContext("/state", ex -> {
			if (!authorised(ex))
			{
				respond(ex, 401, "{\"error\":\"bad token\"}");
				return;
			}
			respond(ex, 200, endpoints.snapshot());
		});

		server.createContext("/widget", ex -> {
			if (!authorised(ex))
			{
				respond(ex, 401, "{\"error\":\"bad token\"}");
				return;
			}
			try
			{
				respond(ex, 200, endpoints.widget(query(ex)));
			}
			catch (Exception e)
			{
				respond(ex, 500, "{\"error\":" + Json.string(String.valueOf(e.getMessage())) + "}");
			}
		});

		server.createContext("/menu", ex -> {
			if (!authorised(ex))
			{
				respond(ex, 401, "{\"error\":\"bad token\"}");
				return;
			}
			try
			{
				respond(ex, 200, endpoints.menu());
			}
			catch (Exception e)
			{
				respond(ex, 500, "{\"error\":" + Json.string(String.valueOf(e.getMessage())) + "}");
			}
		});

		server.createContext("/project", ex -> {
			if (!authorised(ex))
			{
				respond(ex, 401, "{\"error\":\"bad token\"}");
				return;
			}
			try
			{
				respond(ex, 200, endpoints.project(query(ex)));
			}
			catch (Exception e)
			{
				respond(ex, 500, "{\"error\":" + Json.string(String.valueOf(e.getMessage())) + "}");
			}
		});

		server.createContext("/watch", ex -> {
			if (!authorised(ex))
			{
				respond(ex, 401, "{\"error\":\"bad token\"}");
				return;
			}
			respond(ex, 200, endpoints.watch(query(ex)));
		});

		pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(2);
		server.setExecutor(pool);
		server.start();
		log.info("state-link listening on 127.0.0.1:{}", port);
	}

	void stop()
	{
		if (server != null)
		{
			server.stop(0);
			server = null;
		}
		if (pool != null)
		{
			pool.shutdown();
			try
			{
				pool.awaitTermination(1, TimeUnit.SECONDS);
			}
			catch (InterruptedException ignored)
			{
				Thread.currentThread().interrupt();
			}
			pool = null;
		}
	}

	private boolean authorised(HttpExchange ex)
	{
		if (token.isEmpty())
		{
			return true;
		}
		String header = ex.getRequestHeaders().getFirst("X-Token");
		if (token.equals(header))
		{
			return true;
		}
		return token.equals(query(ex).get("token"));
	}

	private static Map<String, String> query(HttpExchange ex)
	{
		Map<String, String> out = new HashMap<>();
		String raw = ex.getRequestURI().getRawQuery();
		if (raw == null || raw.isEmpty())
		{
			return out;
		}
		for (String pair : raw.split("&"))
		{
			int eq = pair.indexOf('=');
			if (eq < 0)
			{
				out.put(decode(pair), "");
			}
			else
			{
				out.put(decode(pair.substring(0, eq)), decode(pair.substring(eq + 1)));
			}
		}
		return out;
	}

	private static String decode(String s)
	{
		try
		{
			return URLDecoder.decode(s, StandardCharsets.UTF_8.name());
		}
		catch (Exception e)
		{
			return s;
		}
	}

	private static void respond(HttpExchange ex, int code, String body) throws IOException
	{
		byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
		ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
		ex.getResponseHeaders().add("Cache-Control", "no-store");
		ex.sendResponseHeaders(code, bytes.length);
		try (OutputStream os = ex.getResponseBody())
		{
			os.write(bytes);
		}
		finally
		{
			ex.close();
		}
	}

}
