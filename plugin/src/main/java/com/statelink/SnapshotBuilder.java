package com.statelink;

import java.awt.Rectangle;
import java.awt.Shape;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import net.runelite.api.Actor;
import net.runelite.api.Client;
import net.runelite.api.GameObject;
import net.runelite.api.InventoryID;
import net.runelite.api.Item;
import net.runelite.api.ItemContainer;
import net.runelite.api.MenuEntry;
import net.runelite.api.NPC;
import net.runelite.api.ObjectComposition;
import net.runelite.api.Player;
import net.runelite.api.Scene;
import net.runelite.api.Skill;
import net.runelite.api.Tile;
import net.runelite.api.TileItem;
import net.runelite.api.TileObject;
import net.runelite.api.coords.LocalPoint;
import net.runelite.api.coords.WorldPoint;
import net.runelite.api.widgets.Widget;
import net.runelite.client.game.ItemManager;

/**
 * Turns the live client into a plain nested map, once per game tick.
 *
 * Everything here runs on the client thread. The rule that shapes the whole
 * design: read once, cheaply, into a structure the HTTP threads can hand out
 * without ever touching the game again.
 *
 * The expensive part is the scene scan for objects, so that one is filtered
 * by name and stays empty until the bot asks for something over /watch.
 * NPCs and ground items are already flat lists, so those come back whole
 * (capped) when no filter is set.
 */
class SnapshotBuilder
{
	private static final int MAX_ENTITIES = 64;
	private static final int SCENE_MAX = 103;
	private static final int LOGGED_IN_STATE = 30;

	private final Client client;
	private final ItemManager itemManager;
	private final StateLinkConfig config;

	SnapshotBuilder(Client client, ItemManager itemManager, StateLinkConfig config)
	{
		this.client = client;
		this.itemManager = itemManager;
		this.config = config;
	}

	Map<String, Object> build(Filters filters, List<Map<String, Object>> chat)
	{
		Map<String, Object> out = new LinkedHashMap<>();
		out.put("tick", client.getTickCount());
		out.put("timestamp", System.currentTimeMillis());
		out.put("gameState", client.getGameState().name());
		out.put("canvas", canvas());

		Player me = client.getLocalPlayer();
		boolean loggedIn = me != null
			&& client.getGameState().getState() == LOGGED_IN_STATE;
		out.put("loggedIn", loggedIn);

		if (!loggedIn)
		{
			out.put("player", null);
			return out;
		}

		out.put("player", player(me));
		out.put("vitals", vitals());
		out.put("skills", skills());
		out.put("inventory", container(InventoryID.INVENTORY, true));
		out.put("equipment", container(InventoryID.EQUIPMENT, false));
		out.put("bank", container(InventoryID.BANK, false));
		out.put("widgets", widgets());
		out.put("npcs", npcs(filters.npcs, me));
		out.put("objects", objects(filters.objects, me));
		out.put("groundItems", groundItems(filters.items, me));
		out.put("players", nearbyPlayers(me));
		out.put("chat", chat);

		if (config.includeMenu())
		{
			out.put("menu", menu());
		}
		return out;
	}

	// ------------------------------------------------------------ player

	private Map<String, Object> player(Player me)
	{
		Map<String, Object> p = new LinkedHashMap<>();
		p.put("name", me.getName());
		p.put("combatLevel", me.getCombatLevel());
		p.put("world", client.getWorld());
		p.put("worldPoint", worldPoint(me.getWorldLocation()));
		p.put("animation", me.getAnimation());
		p.put("poseAnimation", me.getPoseAnimation());
		p.put("idlePoseAnimation", me.getIdlePoseAnimation());
		// The honest way to ask "am I walking": the pose the client is
		// playing right now is not the standing-still pose.
		p.put("moving", me.getPoseAnimation() != me.getIdlePoseAnimation());
		p.put("idle", me.getAnimation() == -1
			&& me.getPoseAnimation() == me.getIdlePoseAnimation());
		p.put("interacting", actorRef(me.getInteracting()));
		p.put("clickbox", shape(me.getConvexHull()));
		return p;
	}

	private Map<String, Object> actorRef(Actor actor)
	{
		if (actor == null)
		{
			return null;
		}
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("name", actor.getName());
		m.put("animation", actor.getAnimation());
		if (actor instanceof NPC)
		{
			m.put("id", ((NPC) actor).getId());
		}
		m.put("healthRatio", actor.getHealthRatio());
		m.put("healthScale", actor.getHealthScale());
		return m;
	}

	private Map<String, Object> vitals()
	{
		Map<String, Object> v = new LinkedHashMap<>();
		int hp = client.getBoostedSkillLevel(Skill.HITPOINTS);
		int maxHp = client.getRealSkillLevel(Skill.HITPOINTS);
		v.put("hitpoints", hp);
		v.put("maxHitpoints", maxHp);
		v.put("hitpointsPercent", maxHp == 0 ? 0 : (100 * hp) / maxHp);
		v.put("prayer", client.getBoostedSkillLevel(Skill.PRAYER));
		v.put("maxPrayer", client.getRealSkillLevel(Skill.PRAYER));

		// getEnergy() moved from 0-100 to 0-10000 at some point; normalise
		// instead of pinning the plugin to one side of that change.
		int energy = client.getEnergy();
		v.put("runEnergy", energy > 100 ? energy / 100 : energy);
		v.put("running", client.getVarpValue(173) == 1);
		// Varp 300 is the special attack meter, in tenths of a percent.
		v.put("specialAttack", client.getVarpValue(300) / 10);
		v.put("weight", client.getWeight());
		return v;
	}

	private Map<String, Object> skills()
	{
		Map<String, Object> out = new LinkedHashMap<>();
		for (Skill skill : Skill.values())
		{
			String name = skill.name().toLowerCase(Locale.ROOT);
			if ("overall".equals(name))
			{
				continue;
			}
			try
			{
				Map<String, Object> s = new LinkedHashMap<>();
				s.put("level", client.getRealSkillLevel(skill));
				s.put("boosted", client.getBoostedSkillLevel(skill));
				s.put("xp", client.getSkillExperience(skill));
				out.put(name, s);
			}
			catch (Exception ignored)
			{
				// A skill this client build does not know about.
			}
		}
		return out;
	}

	// --------------------------------------------------------- containers

	private Map<String, Object> container(InventoryID id, boolean capacity28)
	{
		ItemContainer c = client.getItemContainer(id);
		Map<String, Object> out = new LinkedHashMap<>();
		List<Map<String, Object>> items = new ArrayList<>();
		if (c == null)
		{
			out.put("open", false);
			out.put("items", items);
			out.put("used", 0);
			out.put("empty", true);
			out.put("full", false);
			return out;
		}
		Item[] raw = c.getItems();
		int used = 0;
		for (int slot = 0; slot < raw.length; slot++)
		{
			Item item = raw[slot];
			if (item == null || item.getId() <= 0)
			{
				continue;
			}
			used++;
			Map<String, Object> m = new LinkedHashMap<>();
			m.put("slot", slot);
			m.put("id", item.getId());
			m.put("quantity", item.getQuantity());
			m.put("name", itemName(item.getId()));
			items.add(m);
		}
		out.put("open", true);
		out.put("items", items);
		out.put("used", used);
		out.put("empty", used == 0);
		out.put("full", capacity28 && used >= 28);
		return out;
	}

	private String itemName(int id)
	{
		try
		{
			return itemManager.getItemComposition(id).getName();
		}
		catch (Exception e)
		{
			return "";
		}
	}

	// ------------------------------------------------------------ widgets

	private Map<String, Object> widgets()
	{
		Map<String, Object> w = new LinkedHashMap<>();
		w.put("bank", isOpen(Ids.BANK));
		w.put("bankPin", isOpen(Ids.BANK_PIN));
		w.put("depositBox", isOpen(Ids.DEPOSIT_BOX));
		w.put("grandExchange", isOpen(Ids.GRAND_EXCHANGE));
		w.put("levelUp", isOpen(Ids.LEVEL_UP));
		w.put("logout", isOpen(Ids.LOGOUT_PANEL));
		w.put("production", isOpen(Ids.PRODUCTION));
		w.put("dialogueNpc", isOpen(Ids.DIALOG_NPC));
		w.put("dialoguePlayer", isOpen(Ids.DIALOG_PLAYER));
		w.put("dialogueOption", isOpen(Ids.DIALOG_OPTION));
		w.put("dialogueSprite", isOpen(Ids.DIALOG_SPRITE));
		w.put("dialogueText", dialogueText());
		return w;
	}

	private boolean isOpen(int group)
	{
		Widget root = client.getWidget(group, 0);
		return root != null && !root.isHidden();
	}

	private String dialogueText()
	{
		int[][] candidates = {
			{Ids.DIALOG_NPC, 4},
			{Ids.DIALOG_PLAYER, 4},
			{Ids.DIALOG_SPRITE, 2},
		};
		for (int[] c : candidates)
		{
			Widget w = client.getWidget(c[0], c[1]);
			if (w != null && !w.isHidden() && w.getText() != null && !w.getText().isEmpty())
			{
				return strip(w.getText());
			}
		}
		return "";
	}

	// ----------------------------------------------------------- entities

	private List<Map<String, Object>> npcs(Set<String> filter, Player me)
	{
		List<Map<String, Object>> out = new ArrayList<>();
		WorldPoint origin = me.getWorldLocation();
		int radius = config.radius();
		for (NPC npc : client.getNpcs())
		{
			if (npc == null || npc.getName() == null || out.size() >= MAX_ENTITIES)
			{
				continue;
			}
			if (!matches(filter, npc.getName()))
			{
				continue;
			}
			WorldPoint wp = npc.getWorldLocation();
			if (wp == null || origin.distanceTo(wp) > radius)
			{
				continue;
			}
			Map<String, Object> m = new LinkedHashMap<>();
			m.put("id", npc.getId());
			m.put("name", npc.getName());
			m.put("combatLevel", npc.getCombatLevel());
			m.put("animation", npc.getAnimation());
			m.put("healthRatio", npc.getHealthRatio());
			m.put("healthScale", npc.getHealthScale());
			m.put("dead", npc.getHealthRatio() == 0);
			m.put("interacting", npc.getInteracting() == null
				? null : npc.getInteracting().getName());
			m.put("worldPoint", worldPoint(wp));
			m.put("distance", origin.distanceTo(wp));
			m.put("clickbox", shape(npc.getConvexHull()));
			out.add(m);
		}
		sortByDistance(out);
		return out;
	}

	private List<Map<String, Object>> objects(Set<String> filter, Player me)
	{
		List<Map<String, Object>> out = new ArrayList<>();
		if (filter.isEmpty())
		{
			// A full scene scan every tick for nobody is the one thing here
			// that would genuinely cost frames. Ask for objects by name.
			return out;
		}

		Scene scene = client.getScene();
		Tile[][][] tiles = scene.getTiles();
		int plane = client.getPlane();
		LocalPoint lp = me.getLocalLocation();
		if (lp == null)
		{
			return out;
		}
		int radius = config.radius();
		int cx = lp.getSceneX();
		int cy = lp.getSceneY();
		WorldPoint origin = me.getWorldLocation();

		for (int x = Math.max(0, cx - radius); x <= Math.min(SCENE_MAX, cx + radius); x++)
		{
			for (int y = Math.max(0, cy - radius); y <= Math.min(SCENE_MAX, cy + radius); y++)
			{
				Tile tile = tiles[plane][x][y];
				if (tile == null)
				{
					continue;
				}
				GameObject[] gameObjects = tile.getGameObjects();
				if (gameObjects != null)
				{
					for (GameObject go : gameObjects)
					{
						addObject(out, filter, go, origin);
					}
				}
				addObject(out, filter, tile.getWallObject(), origin);
				addObject(out, filter, tile.getDecorativeObject(), origin);
				addObject(out, filter, tile.getGroundObject(), origin);
			}
		}
		sortByDistance(out);
		return out;
	}

	private void addObject(List<Map<String, Object>> out, Set<String> filter,
						   TileObject obj, WorldPoint origin)
	{
		if (obj == null || out.size() >= MAX_ENTITIES)
		{
			return;
		}
		ObjectComposition def = composition(obj.getId());
		if (def == null || def.getName() == null || "null".equals(def.getName()))
		{
			return;
		}
		if (!matches(filter, def.getName()))
		{
			return;
		}
		WorldPoint wp = obj.getWorldLocation();
		if (wp == null)
		{
			return;
		}
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("id", obj.getId());
		m.put("name", def.getName());
		m.put("actions", actions(def));
		m.put("worldPoint", worldPoint(wp));
		m.put("distance", origin.distanceTo(wp));

		Shape click = null;
		if (obj instanceof GameObject)
		{
			click = ((GameObject) obj).getClickbox();
		}
		if (click == null)
		{
			click = obj.getCanvasTilePoly();
		}
		m.put("clickbox", shape(click));
		out.add(m);
	}

	/**
	 * Bank booths, doors and anything else that swaps appearance by varbit
	 * report a placeholder id. getImpostor() resolves it to what the player
	 * is actually looking at, and only works on the client thread - which is
	 * most of the reason this class exists at all.
	 */
	private ObjectComposition composition(int id)
	{
		try
		{
			ObjectComposition def = client.getObjectDefinition(id);
			if (def == null)
			{
				return null;
			}
			return def.getImpostorIds() == null ? def : def.getImpostor();
		}
		catch (Exception e)
		{
			return null;
		}
	}

	private List<String> actions(ObjectComposition def)
	{
		List<String> out = new ArrayList<>();
		String[] raw = def.getActions();
		if (raw != null)
		{
			for (String a : raw)
			{
				if (a != null && !a.isEmpty())
				{
					out.add(a);
				}
			}
		}
		return out;
	}

	private List<Map<String, Object>> groundItems(Set<String> filter, Player me)
	{
		List<Map<String, Object>> out = new ArrayList<>();
		Scene scene = client.getScene();
		Tile[][][] tiles = scene.getTiles();
		int plane = client.getPlane();
		LocalPoint lp = me.getLocalLocation();
		if (lp == null)
		{
			return out;
		}
		int radius = Math.min(config.radius(), 12);
		int cx = lp.getSceneX();
		int cy = lp.getSceneY();
		WorldPoint origin = me.getWorldLocation();

		for (int x = Math.max(0, cx - radius); x <= Math.min(SCENE_MAX, cx + radius); x++)
		{
			for (int y = Math.max(0, cy - radius); y <= Math.min(SCENE_MAX, cy + radius); y++)
			{
				Tile tile = tiles[plane][x][y];
				if (tile == null || tile.getGroundItems() == null)
				{
					continue;
				}
				for (TileItem item : tile.getGroundItems())
				{
					if (item == null || out.size() >= MAX_ENTITIES)
					{
						continue;
					}
					String name = itemName(item.getId());
					if (name.isEmpty() || !matches(filter, name))
					{
						continue;
					}
					Map<String, Object> m = new LinkedHashMap<>();
					m.put("id", item.getId());
					m.put("name", name);
					m.put("quantity", item.getQuantity());
					m.put("worldPoint", worldPoint(tile.getWorldLocation()));
					m.put("distance", origin.distanceTo(tile.getWorldLocation()));
					m.put("clickbox", shape(tile.getItemLayer() == null
						? null : tile.getItemLayer().getCanvasTilePoly()));
					out.add(m);
				}
			}
		}
		sortByDistance(out);
		return out;
	}

	private List<Map<String, Object>> nearbyPlayers(Player me)
	{
		List<Map<String, Object>> out = new ArrayList<>();
		WorldPoint origin = me.getWorldLocation();
		for (Player p : client.getPlayers())
		{
			if (p == null || p == me || p.getName() == null || out.size() >= MAX_ENTITIES)
			{
				continue;
			}
			WorldPoint wp = p.getWorldLocation();
			Map<String, Object> m = new LinkedHashMap<>();
			m.put("name", p.getName());
			m.put("combatLevel", p.getCombatLevel());
			m.put("distance", wp == null ? 999 : origin.distanceTo(wp));
			m.put("interactingWithMe", p.getInteracting() == me);
			out.add(m);
		}
		sortByDistance(out);
		return out;
	}

	// -------------------------------------------------------------- menu

	Map<String, Object> menu()
	{
		Map<String, Object> m = new LinkedHashMap<>();
		boolean open = client.isMenuOpen();
		m.put("open", open);
		if (open)
		{
			// The bot needs these to click a row of an open right-click menu:
			// rows start one header height below the top and are evenly spaced.
			m.put("bounds", rect(new Rectangle(client.getMenuX(), client.getMenuY(),
				client.getMenuWidth(), client.getMenuHeight())));
		}
		else
		{
			m.put("bounds", null);
		}
		List<Map<String, Object>> entries = new ArrayList<>();
		MenuEntry[] raw = client.getMenuEntries();
		if (raw != null)
		{
			// RuneLite hands these back bottom-first, so the top of the
			// visible menu - what a plain left click actually does - is the
			// last element. Reversed here so index 0 is the left-click action.
			for (int i = raw.length - 1; i >= 0; i--)
			{
				MenuEntry e = raw[i];
				if (e == null)
				{
					continue;
				}
				Map<String, Object> entry = new LinkedHashMap<>();
				entry.put("option", strip(e.getOption()));
				entry.put("target", strip(e.getTarget()));
				entries.add(entry);
			}
		}
		m.put("entries", entries);
		return m;
	}

	// ------------------------------------------------------------ shapes

	private Map<String, Object> canvas()
	{
		Map<String, Object> m = new LinkedHashMap<>();
		double[] scale = stretchScale();
		m.put("scaleX", scale[0]);
		m.put("scaleY", scale[1]);
		try
		{
			java.awt.Canvas c = client.getCanvas();
			m.put("width", c.getWidth());
			m.put("height", c.getHeight());
			m.put("focused", c.isFocusOwner());
			java.awt.Point origin = c.getLocationOnScreen();
			// This is what lets the bot turn a canvas point straight into a
			// screen point, without going hunting for the window itself.
			m.put("screenX", origin.x);
			m.put("screenY", origin.y);
			m.put("onScreen", true);
		}
		catch (Exception e)
		{
			m.put("onScreen", false);
		}
		return m;
	}

	/**
	 * How far the rendered canvas is blown up past the game's own resolution.
	 *
	 * With RuneLite's Stretched Mode on, the game still draws - and still
	 * reports every widget bound, hitbox and minimap point - in its own
	 * unstretched coordinate space, while the canvas on screen is larger.
	 * Without this factor a bot aims at the unscaled position and misses by
	 * more the further it is from the top-left corner.
	 *
	 * If this does not compile against your RuneLite version, the whole
	 * feature is optional: return {1.0, 1.0} and do not use Stretched Mode.
	 */
	private double[] stretchScale()
	{
		try
		{
			if (!client.isStretchedEnabled())
			{
				return new double[]{1.0, 1.0};
			}
			java.awt.Dimension real = client.getRealDimensions();
			java.awt.Dimension stretched = client.getStretchedDimensions();
			if (real == null || stretched == null
				|| real.width <= 0 || real.height <= 0)
			{
				return new double[]{1.0, 1.0};
			}
			return new double[]{
				stretched.getWidth() / real.getWidth(),
				stretched.getHeight() / real.getHeight()};
		}
		catch (Exception e)
		{
			return new double[]{1.0, 1.0};
		}
	}

	private static void sortByDistance(List<Map<String, Object>> list)
	{
		list.sort((a, b) -> Integer.compare(
			((Number) a.get("distance")).intValue(),
			((Number) b.get("distance")).intValue()));
	}

	private static Map<String, Object> shape(Shape s)
	{
		return s == null ? null : rect(s.getBounds());
	}

	private static Map<String, Object> rect(Rectangle r)
	{
		if (r == null || r.width <= 0 || r.height <= 0)
		{
			return null;
		}
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("x", r.x);
		m.put("y", r.y);
		m.put("w", r.width);
		m.put("h", r.height);
		m.put("cx", r.x + r.width / 2);
		m.put("cy", r.y + r.height / 2);
		return m;
	}

	private static Map<String, Object> worldPoint(WorldPoint wp)
	{
		if (wp == null)
		{
			return null;
		}
		Map<String, Object> m = new LinkedHashMap<>();
		m.put("x", wp.getX());
		m.put("y", wp.getY());
		m.put("plane", wp.getPlane());
		return m;
	}

	static String strip(String s)
	{
		return s == null ? "" : s.replaceAll("<[^>]*>", "");
	}

	private static boolean matches(Set<String> filter, String name)
	{
		if (filter.isEmpty())
		{
			return true;
		}
		String lower = name.toLowerCase(Locale.ROOT);
		for (String want : filter)
		{
			if (lower.equals(want) || lower.contains(want))
			{
				return true;
			}
		}
		return false;
	}

	/** The name filters the bot has asked for, set over /watch. */
	static final class Filters
	{
		final Set<String> objects;
		final Set<String> npcs;
		final Set<String> items;

		Filters(Set<String> objects, Set<String> npcs, Set<String> items)
		{
			this.objects = objects;
			this.npcs = npcs;
			this.items = items;
		}

		static Filters none()
		{
			return new Filters(Collections.emptySet(),
				Collections.emptySet(), Collections.emptySet());
		}
	}

	static Set<String> parseNames(String csv)
	{
		Set<String> out = new LinkedHashSet<>();
		if (csv == null)
		{
			return out;
		}
		for (String part : csv.split(","))
		{
			String trimmed = part.trim().toLowerCase(Locale.ROOT);
			if (!trimmed.isEmpty())
			{
				out.add(trimmed);
			}
		}
		return out;
	}
}
