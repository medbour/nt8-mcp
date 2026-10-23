// NT8Bridge.Editor.cs — POST /editor/open: open the NinjaScript Editor window, as Control Center > New >
// NinjaScript Editor does. Part of the NT8Bridge AddOn (core: NT8Bridge.cs; seams: addon/NOTES.md "Module seams").
// Contract: docs/api/compile.md "Opening the editor".
//
// Why: the F5 compile path (nt_compile_f5) needs an open editor window, and before this only a human could open one.
//
// Mechanism: ControlCenter.OnNinjaScriptEditor(object, RoutedEventArgs) is the Click handler that NT8 wires to the
// Control Center's New > NinjaScript Editor menu item (NinjaTrader.Gui.dll, internal instance method). It is
// invoked by reflection on the Control Center's own dispatcher with a MenuItem.ClickEvent args object — the same
// call the menu click makes. The method bodies in NinjaTrader.Gui are encrypted, so the handler is used as a
// black box; the result is verified by looking for a NinjaTrader.Gui.NinjaScript.Editor.EditorView window.
//
// It opens a window and nothing else: no compile, no save, no close, no F5. An editor already open is left alone.

#region Using declarations
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using System.Windows;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public partial class NT8Bridge
	{
		/// <summary>ControlCenter.OnNinjaScriptEditor(object, RoutedEventArgs), resolved once in Start_Editor.</summary>
		private static MethodInfo Editor_onMenu;
		/// <summary>One open at a time: two quick requests must not make two editors.</summary>
		private static readonly object Editor_gate = new object();
		private static readonly TimeSpan Editor_UiTimeout = TimeSpan.FromSeconds(10);
		private const int Editor_WaitMs = 10000, Editor_PollMs = 250;

		private static string Route_Editor(string method, string[] seg, System.Collections.Specialized.NameValueCollection q, string body, ref int status)
		{
			if (method == "POST" && seg.Length == 2 && seg[0] == "editor" && seg[1] == "open") return Editor_Open(ref status);
			return null;
		}

		private static void Start_Editor()
		{
			Editor_onMenu = Compat.Resolve("ControlCenter.OnNinjaScriptEditor", () =>
				typeof(Gui.ControlCenter).GetMethod("OnNinjaScriptEditor",
					BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null,
					new[] { typeof(object), typeof(RoutedEventArgs) }, null)) as MethodInfo;
		}
		// No Stop_Editor: no event, no thread.

		private static string Editor_Open(ref int status)
		{
			if (!Monitor.TryEnter(Editor_gate)) return Err(ref status, 409, "an editor open is already running");
			try
			{
				string title = Editor_OpenTitle();
				if (title != null) return Editor_Result(true, title);

				if (Editor_onMenu == null)
					return Err(ref status, 503, "ControlCenter.OnNinjaScriptEditor did not resolve — see GET /compat");
				Window cc = Sr_ControlCenter();
				if (cc == null) return Err(ref status, 503, "no Control Center window in the registry");

				// Never throw on the UI thread: the handler's own exception comes back as a string.
				string uiErr = Ui(cc.Dispatcher, () =>
				{
					try
					{
						Editor_onMenu.Invoke(cc, new object[] { cc, new RoutedEventArgs(System.Windows.Controls.MenuItem.ClickEvent) });
						return null;
					}
					catch (Exception ex) { return Deep(ex); }
				}, Editor_UiTimeout, "ControlCenter");
				if (uiErr != null) { Log("editor/open: " + uiErr); return Err(ref status, 500, "OnNinjaScriptEditor threw: " + uiErr); }

				// The window may be built on its own UI thread a moment later: poll from here, never from a dispatcher.
				for (int waited = 0; waited <= Editor_WaitMs; waited += Editor_PollMs)
				{
					title = Editor_OpenTitle();
					if (title != null) { Log("editor/open: opened '" + title + "'"); return Editor_Result(false, title); }
					Thread.Sleep(Editor_PollMs);
				}
				return Err(ref status, 504, "the menu handler ran but no NinjaScript Editor window appeared within " + (Editor_WaitMs / 1000) + " s");
			}
			catch (Exception ex) { Log("editor/open: " + Deep(ex)); return Err(ref status, 500, Deep(ex)); }
			finally { Monitor.Exit(Editor_gate); }
		}

		private static string Editor_Result(bool alreadyOpen, string title)
		{
			return Obj(P("ok", "true"), P("alreadyOpen", alreadyOpen ? "true" : "false"), P("windowTitle", Q(title)));
		}

		/// <summary>Title of an open NinjaScript Editor window (one with a live handle), else null. Never throws.</summary>
		private static string Editor_OpenTitle()
		{
			try
			{
				HashSet<Window> owned;
				foreach (var w in KnownWindows(out owned))
				{
					if (!(w is Gui.NinjaScript.Editor.EditorView)) continue;
					try
					{
						var win = w;
						string t = Ui(win.Dispatcher, () =>
							new System.Windows.Interop.WindowInteropHelper(win).Handle != IntPtr.Zero ? TitleOf(win) : null,
							TimeSpan.FromSeconds(2), "EditorView");
						if (t != null) return t.Length > 0 ? t : "NinjaScript Editor";
					}
					catch { return "NinjaScript Editor"; }	// its thread is busy, but it exists: never open a second one
				}
			}
			catch { }
			return null;
		}
	}
}
