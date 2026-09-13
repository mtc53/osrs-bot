package com.statelink;

import java.util.Map;

/**
 * A 60-line JSON writer for maps, lists and primitives.
 *
 * RuneLite does ship Gson, but its version has moved around between
 * releases and the snapshot is plain nested maps anyway, so writing the
 * text directly costs less than carrying the dependency.
 */
final class Json
{
	static String write(Object value)
	{
		StringBuilder sb = new StringBuilder(4096);
		append(sb, value);
		return sb.toString();
	}

	private static void append(StringBuilder sb, Object value)
	{
		if (value == null)
		{
			sb.append("null");
		}
		else if (value instanceof Map)
		{
			sb.append('{');
			boolean first = true;
			for (Map.Entry<?, ?> e : ((Map<?, ?>) value).entrySet())
			{
				if (!first)
				{
					sb.append(',');
				}
				first = false;
				sb.append(string(String.valueOf(e.getKey()))).append(':');
				append(sb, e.getValue());
			}
			sb.append('}');
		}
		else if (value instanceof Iterable)
		{
			sb.append('[');
			boolean first = true;
			for (Object o : (Iterable<?>) value)
			{
				if (!first)
				{
					sb.append(',');
				}
				first = false;
				append(sb, o);
			}
			sb.append(']');
		}
		else if (value instanceof Boolean)
		{
			sb.append(value.toString());
		}
		else if (value instanceof Number)
		{
			double d = ((Number) value).doubleValue();
			if (Double.isNaN(d) || Double.isInfinite(d))
			{
				sb.append("null");
			}
			else
			{
				sb.append(value.toString());
			}
		}
		else
		{
			sb.append(string(value.toString()));
		}
	}

	static String string(String s)
	{
		StringBuilder sb = new StringBuilder(s.length() + 16);
		sb.append('"');
		for (int i = 0; i < s.length(); i++)
		{
			char c = s.charAt(i);
			switch (c)
			{
				case '"':
					sb.append("\\\"");
					break;
				case '\':
					sb.append("\\\\");
					break;
				case '\n':
					sb.append("\n");
					break;
				case '\r':
					sb.append("\r");
					break;
				case '\t':
					sb.append("\t");
					break;
				default:
					if (c < 0x20 || c == 0x7f)
					{
						sb.append(String.format("\u%04x", (int) c));
					}
					else
					{
						sb.append(c);
					}
			}
		}
		return sb.append('"').toString();
	}

	private Json()
	{
	}
}
