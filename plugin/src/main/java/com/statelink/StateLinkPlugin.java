package com.statelink;

import com.google.inject.Provides;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import javax.inject.Inject;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.Perspective;
import net.runelite.api.Point;
import net.runelite.api.coords.LocalPoint;
import net.runelite.api.coords.WorldPoint;
import net.runelite.api.events.ChatMessage;
import net.runelite.api.events.GameTick;
import net.runelite.api.widgets.Widget;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.events.ConfigChanged;
import net.runelite.client.game.ItemManager;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;

/**
 * Exports the game state a bot needs over a loopback HTTP port.
 *
 * The split this plugin exists to enable: RuneLite is the only thing that
 * can honestly answer "what is on screen right now", so it answers that and
 * nothing else. It sends no input and takes no actions - the process on the
 * other end of the socket owns every click. Keeping the two halves apart is
 * also what keeps this side simple enough to audit in one sitting.
 */
@Slf4j
@PluginDescriptor(
	name = "State Link",
	description = "Publishes live game state on a loopback HTTP port",
	tags = {"automation", "state", "api"}
)
public class StateLinkPlugin extends Plugin implements StateServer.Endpoints
{
	private static final int CHAT_HISTORY = 24;
	private static final long WIDGET_TIMEOUT_MS = 2000;

	@Inject
	private Client client;

	@Inject
	private ClientThread clientThread;

	@Inject
	private ItemManager itemManager;

	@Inject
	private StateLinkConfig config;

	private SnapshotBuilder builder;
	private StateServer server;

	/**
	 * Written on the client thread once a tick, read by HTTP threads. The
	 * volatile reference is the entire synchronisation story: publishing a
	 * fresh immutable string is atomic, so readers either get last tick or
	 * this tick, never a half-built one.
	 */
	private volatile String latest = "{\"loggedIn\":false,\"note\":\"no snapshot yet\"}";

	private volatile SnapshotBuilder.Filters filters = SnapshotBuilder.Filters.none();

	private final Deque<Map<String, Object>> chat = new ArrayDeque<>();

	@Provides
	StateLinkConfig provideConfig(ConfigManager configManager)
	{
		return configManager.getConfig(StateLinkConfig.class);
	}

	@Override
	protected void startUp() throws Exception
	{
		builder = new SnapshotBuilder(client, itemManager, config);
		startServer();
	}

	@Override
	protected void shutDown()
	{
		stopServer();
		chat.clear();
		latest = "{\"loggedIn\":false,\"note\":\"plugin stopped\"}";
	}

	private void startServer() throws Exception
	{
		server = new StateServer(config.port(), config.token(), this);
		server.start();
	}

	private void stopServer()
	{
		if (server != null)
		{
			server.stop();
			server = null;
		}
	}

	@Subscribe
	public void onConfigChanged(ConfigChanged event)
	{
		if (!"statelink".equals(event.getGroup()))
		{
			return;
		}
		if ("port".equals(event.getKey()) || "token".equals(event.getKey()))
		{
			stopServer();
			try
			{
				startServer();
			}
			catch (Exception e)
			{
				log.warn("could not restart the state server", e);
			}
		}
	}

	@Subscribe
	public void onGameTick(GameTick tick)
	{
		// Already on the client thread here, so this is the one place the
		// game may be read directly.
		try
		{
			latest = Json.write(builder.build(filters, new ArrayList<>(chat)));
		}
		catch (Exception e)
		{
			log.warn("snapshot failed", e);
			latest = "{\"error\":" + Json.string(String.valueOf(e.getMessage())) + "}";
		}
	}

	@Subscribe
	public void onChatMessage(ChatMessage event)
	{
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("type", event.getType().name());
		m.put("sender", event.getName() == null ? "" : SnapshotBuilder.strip(event.getName()));
		m.put("message", SnapshotBuilder.strip(event.getMessage()));
		m.put("timestamp", System.currentTimeMillis());
		chat.addLast(m);
		while (chat.size() > CHAT_HISTORY)
		{
			chat.removeFirst();
		}
	}

	// -------------------------------------------------------- endpoints

	@Override
	public String snapshot()
	{
		return latest;
	}

	/**
	 * Live widget lookup, for the interfaces that are too numerous to export
	 * every tick - a bank slot, a make-X button, one line of a dialogue.
	 *
	 * This is the only read that leaves the tick loop, so it hands the work
	 * back to the client thread and waits on it rather than reaching into
	 * the client from an HTTP worker.
	 */
	@Override
	public String widget(Map<String, String> query) throws Exception
	{
		int group = intArg(query, "group", -1);
		int child = intArg(query, "child", 0);
		int sub = intArg(query, "sub", -1);
		if (group < 0)
		{
			return "{\"error\":\"group is required\"}";
		}

		CompletableFuture<String> future = new CompletableFuture<>();
		clientThread.invoke(() -> {
			try
			{
				future.complete(Json.write(describeWidget(group, child, sub)));
			}
			catch (Exception e)
			{
				future.completeExceptionally(e);
			}
		});
		return future.get(WIDGET_TIMEOUT_MS, TimeUnit.MILLISECONDS);
	}

	private Map<String, Object> describeWidget(int group, int child, int sub)
	{
		Map<String, Object> out = new LinkedHashMap<>();
		Widget w = client.getWidget(group, child);
		if (w != null && sub >= 0)
		{
			Widget[] children = w.getDynamicChildren();
			w = (children != null && sub < children.length) ? children[sub] : w.getChild(sub);
		}
		if (w == null)
		{
			out.put("found", false);
			return out;
		}
		out.put("found", true);
		out.put("hidden", w.isHidden());
		out.put("text", SnapshotBuilder.strip(w.getText()));
		out.put("name", SnapshotBuilder.strip(w.getName()));
		out.put("itemId", w.getItemId());
		out.put("itemQuantity", w.getItemQuantity());
		java.awt.Rectangle b = w.getBounds();
		if (b != null && b.width > 0 && b.height > 0)
		{
			Map<String, Object> bounds = new LinkedHashMap<>();
			bounds.put("x", b.x);
			bounds.put("y", b.y);
			bounds.put("w", b.width);
			bounds.put("h", b.height);
			bounds.put("cx", b.x + b.width / 2);
			bounds.put("cy", b.y + b.height / 2);
			out.put("bounds", bounds);
		}
		else
		{
			out.put("bounds", null);
		}

		Widget[] dynamic = w.getDynamicChildren();
		out.put("dynamicChildren", dynamic == null ? 0 : dynamic.length);

		// Dialogue option lists are the common case for wanting the children
		// themselves rather than a count, so ship their text along.
		List<String> texts = new ArrayList<>();
		Widget[] statics = w.getChildren();
		if (statics != null)
		{
			for (Widget c : statics)
			{
				if (c != null && c.getText() != null && !c.getText().isEmpty())
				{
					texts.add(SnapshotBuilder.strip(c.getText()));
				}
				if (texts.size() >= 32)
				{
					break;
				}
			}
		}
		out.put("childTexts", texts);
		return out;
	}

	/**
	 * The right-click menu, read live rather than at tick boundaries.
	 *
	 * Hover-verify-click is the whole reliability story for this bot: point
	 * at a thing, ask the client what a click would actually do, and only
	 * then commit. Waiting up to 600ms for the next tick to find that out
	 * would make every interaction cost an extra tick, so this one reads
	 * through to the client thread on demand.
	 */
	@Override
	public String menu() throws Exception
	{
		CompletableFuture<String> future = new CompletableFuture<>();
		clientThread.invoke(() -> {
			try
			{
				future.complete(Json.write(builder.menu()));
			}
			catch (Exception e)
			{
				future.completeExceptionally(e);
			}
		});
		return future.get(WIDGET_TIMEOUT_MS, TimeUnit.MILLISECONDS);
	}

	/**
	 * Where a world tile is drawn - on the canvas, and on the minimap.
	 *
	 * The minimap point is the one that matters: clicking it is how you
	 * walk. Only tiles inside the loaded scene can be projected, so a
	 * long trip has to be walked as a series of hops, not one click.
	 */
	@Override
	public String project(Map<String, String> query) throws Exception
	{
		int x = intArg(query, "x", -1);
		int y = intArg(query, "y", -1);
		int plane = intArg(query, "plane", 0);
		if (x < 0 || y < 0)
		{
			return "{\"error\":\"x and y are required\"}";
		}

		CompletableFuture<String> future = new CompletableFuture<>();
		clientThread.invoke(() -> {
			try
			{
				future.complete(Json.write(projectPoint(x, y, plane)));
			}
			catch (Exception e)
			{
				future.completeExceptionally(e);
			}
		});
		return future.get(WIDGET_TIMEOUT_MS, TimeUnit.MILLISECONDS);
	}

	private Map<String, Object> projectPoint(int x, int y, int plane)
	{
		Map<String, Object> out = new LinkedHashMap<>();
		WorldPoint wp = new WorldPoint(x, y, plane);
		LocalPoint lp = LocalPoint.fromWorld(client, wp);
		if (lp == null)
		{
			out.put("inScene", false);
			out.put("canvas", null);
			out.put("minimap", null);
			return out;
		}
		out.put("inScene", true);
		out.put("canvas", boxAround(Perspective.localToCanvas(client, lp, plane), 12));
		// The generous distance cap lets targets near the edge of the
		// minimap still project, which is what makes hop-walking work.
		out.put("minimap", boxAround(
			Perspective.localToMinimap(client, lp, 6400), 5));
		return out;
	}

	private static Map<String, Object> boxAround(Point point, int half)
	{
		if (point == null)
		{
			return null;
		}
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("x", point.getX() - half);
		m.put("y", point.getY() - half);
		m.put("w", half * 2);
		m.put("h", half * 2);
		m.put("cx", point.getX());
		m.put("cy", point.getY());
		return m;
	}

	@Override
	public String watch(Map<String, String> query)
	{
		filters = new SnapshotBuilder.Filters(
			SnapshotBuilder.parseNames(query.get("objects")),
			SnapshotBuilder.parseNames(query.get("npcs")),
			SnapshotBuilder.parseNames(query.get("items")));

		Map<String, Object> out = new LinkedHashMap<>();
		out.put("objects", filters.objects);
		out.put("npcs", filters.npcs);
		out.put("items", filters.items);
		return Json.write(out);
	}

	private static int intArg(Map<String, String> query, String key, int fallback)
	{
		String raw = query.get(key);
		if (raw == null || raw.isEmpty())
		{
			return fallback;
		}
		try
		{
			return Integer.parseInt(raw.trim());
		}
		catch (NumberFormatException e)
		{
			return fallback;
		}
	}
}
