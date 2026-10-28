import { Link } from "react-router-dom";
import {
  BarChart3,
  BookOpen,
  Clock,
  Cpu,
  KeyRound,
  NotebookText,
  Package,
  Plug,
  Radio,
  Settings,
  ShieldCheck,
  Users,
  Webhook,
  Wrench,
} from "lucide-react";
import type { ComponentType } from "react";
import { Card, CardContent } from "@vendor/ui/ui/components/card";
import { H2 } from "@vendor/ui/ui/components/typography/h2";

type SettingsLink = {
  to: string;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
};

type SettingsGroup = { title: string; links: SettingsLink[] };

const GROUPS: SettingsGroup[] = [
  {
    title: "Agent",
    links: [
      { to: "/models", label: "Models", description: "Choose the model and provider", icon: Cpu },
      { to: "/skills", label: "Skills", description: "Manage installed agent skills", icon: Package },
      { to: "/profiles", label: "Profiles", description: "Switch and edit agent profiles", icon: Users },
      { to: "/config", label: "Config", description: "Edit application settings", icon: Settings },
      { to: "/env", label: "Keys", description: "API keys and environment values", icon: KeyRound },
    ],
  },
  {
    title: "Automation",
    links: [
      { to: "/cron", label: "Cron", description: "Scheduled agent jobs", icon: Clock },
      { to: "/mcp", label: "MCP", description: "Connected MCP servers and tools", icon: Plug },
      { to: "/webhooks", label: "Webhooks", description: "Incoming webhook triggers", icon: Webhook },
      { to: "/channels", label: "Channels", description: "Messaging platform status", icon: Radio },
      { to: "/pairing", label: "Pairing", description: "Paired messaging users", icon: ShieldCheck },
    ],
  },
  {
    title: "App",
    links: [
      { to: "/analytics", label: "Analytics", description: "Usage statistics and trends", icon: BarChart3 },
      { to: "/notes", label: "Notes", description: "Your saved notes", icon: NotebookText },
      { to: "/system", label: "System", description: "Status, diagnostics and operations", icon: Wrench },
      { to: "/docs", label: "Documentation", description: "Read the ARIA documentation", icon: BookOpen },
    ],
  },
];

export default function SettingsPage() {
  return (
    <div className="flex flex-col gap-8">
      <H2 variant="sm" className="text-muted-foreground">
        Settings
      </H2>
      {GROUPS.map((group) => (
        <div key={group.title} className="flex flex-col gap-3">
          <H2 variant="sm" className="text-muted-foreground">
            {group.title}
          </H2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.links.map(({ to, label, description, icon: Icon }) => (
              <Link key={to} to={to} className="block">
                <Card className="h-full border-border transition-colors hover:border-primary/50">
                  <CardContent className="flex items-start gap-3 p-4">
                    <Icon className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="text-sm font-medium">{label}</span>
                      <span className="text-xs text-muted-foreground">{description}</span>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
