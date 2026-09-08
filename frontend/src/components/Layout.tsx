import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@/hooks/useApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { SystemStatus } from "@/lib/api";
import { ConnectionDot, RiskBadge } from "./ui";
import { time } from "@/lib/format";

const PAGES: Array<[string, string]> = [
  ["/", "Overview"],
  ["/pairs", "Hedge Pairs"],
  ["/calculator", "Hedge Calculator"],
  ["/risk", "Risk Dashboard"],
  ["/orders", "Orders"],
  ["/cycles", "Hedge Cycles"],
  ["/configuration", "Configuration"],
  ["/faults", "Fault Injection"],
];

export function Layout() {
  const { data: status } = useQuery<SystemStatus>("/system/status", 4000);
  const { connected } = useWebSocket(["system"]);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <h1>HedgeLab</h1>
          <div className="tag">perpetual ↔ mt5 hedging</div>
        </div>
        <nav className="nav">
          {PAGES.map(([path, label]) => (
            <NavLink key={path} to={path} end={path === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="main">
        <header className="topbar">
          <span className="paper-banner">PAPER TRADING ONLY</span>
          {status ? (
            <>
              <span className="small muted">
                mode {status.trading_mode} · {status.live_adapters_registered} live
                adapters registered
              </span>
              <span className="small">
                <ConnectionDot connected={status.database_connected} />
                database
              </span>
              {status.venues.map((venue) => (
                <span key={venue.venue} className="small">
                  <ConnectionDot connected={venue.connected} />
                  {venue.venue}
                </span>
              ))}
              <span className="small">
                <ConnectionDot connected={connected} />
                live feed
              </span>
              <span className="small muted mono">
                clock {time(status.simulator_clock)}
              </span>
              {status.kill_switch_engaged ? (
                <RiskBadge level="KILL_SWITCH" />
              ) : status.emergency_mode ? (
                <RiskBadge level="EMERGENCY" />
              ) : status.trading_paused ? (
                <RiskBadge level="DANGER" />
              ) : null}
            </>
          ) : (
            <span className="small muted">connecting…</span>
          )}
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
