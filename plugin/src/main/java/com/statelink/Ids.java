package com.statelink;

/**
 * Interface group ids, spelled out here rather than pulled from RuneLite's
 * WidgetInfo / ComponentID.
 *
 * Those constants have been renamed twice in recent client versions, and a
 * plugin that names them fails to compile against half the releases in the
 * wild. The numbers themselves have not moved in years, so this is the more
 * stable dependency even though it looks like the cruder one.
 */
final class Ids
{
	static final int BANK = 12;
	static final int BANK_INVENTORY = 15;
	static final int BANK_PIN = 213;
	static final int INVENTORY = 149;
	static final int DEPOSIT_BOX = 192;
	static final int GRAND_EXCHANGE = 465;
	static final int LEVEL_UP = 233;
	static final int DIALOG_NPC = 231;
	static final int DIALOG_PLAYER = 217;
	static final int DIALOG_OPTION = 219;
	static final int DIALOG_SPRITE = 193;
	static final int LOGOUT_PANEL = 182;
	static final int WORLD_SWITCHER = 69;
	static final int CHATBOX = 162;
	static final int PRODUCTION = 270;

	private Ids()
	{
	}
}
