package com.statelink;

import net.runelite.client.config.Config;
import net.runelite.client.config.ConfigGroup;
import net.runelite.client.config.ConfigItem;

@ConfigGroup("statelink")
public interface StateLinkConfig extends Config
{
	@ConfigItem(
		keyName = "port",
		name = "HTTP port",
		description = "Loopback port the state server listens on. Change it if you run two clients.",
		position = 1
	)
	default int port()
	{
		return 8095;
	}

	@ConfigItem(
		keyName = "token",
		name = "Auth token",
		description = "Optional shared secret. When set, requests must send it as ?token= or an X-Token header.",
		position = 2
	)
	default String token()
	{
		return "";
	}

	@ConfigItem(
		keyName = "radius",
		name = "Scan radius (tiles)",
		description = "How far around the player to scan for objects, NPCs and ground items.",
		position = 3
	)
	default int radius()
	{
		return 16;
	}

	@ConfigItem(
		keyName = "includeMenu",
		name = "Export menu entries",
		description = "Export the right-click menu options currently under the cursor. Needed for hover-verify-click.",
		position = 4
	)
	default boolean includeMenu()
	{
		return true;
	}
}
