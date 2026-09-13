package com.statelink;

import net.runelite.client.RuneLite;
import net.runelite.client.externalplugins.ExternalPluginManager;

/**
 * Dev launcher: starts a real RuneLite with this plugin loaded, so you can
 * iterate without installing anything. Run this from your IDE.
 */
public class StateLinkPluginTest
{
	@SuppressWarnings("unchecked")
	public static void main(String[] args) throws Exception
	{
		ExternalPluginManager.loadBuiltin(StateLinkPlugin.class);
		RuneLite.main(args);
	}
}
