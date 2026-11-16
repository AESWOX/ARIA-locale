import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  PlugZap,
  Radio,
  RotateCw,
  WifiOff,
} from "lucide-react";
import { Badge } from "@vendor/ui/ui/components/badge";
import { Button } from "@vendor/ui/ui/components/button";
import { Card, CardContent } from "@vendor/ui/ui/components/card";
import { Spinner } from "@vendor/ui/ui/components/spinner";
import { Toast } from "@vendor/ui/ui/components/toast";
import { useToast } from "@vendor/ui/hooks/use-toast";
import { api } from "@/lib/api";
import type {
  MessagingPlatform,
} from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";

// State → badge mapping. The backend emits a small, fixed vocabulary plus
// whatever the live gateway runtime reports (connected/disconnected/fatal).
const STATE_BADGE: Record<
  string,
  { tone: "success" | "warning" | "destructive" | "secondary" | "outline"; label: string }
> = {
  connected: { tone: "success", label: "Connected" },
  pending_restart: { tone: "warning", label: "Restart to apply" },
  gateway_stopped: { tone: "warning", label: "Gateway stopped" },
  startup_failed: { tone: "destructive", label: "Start failed" },
  disconnected: { tone: "warning", label: "Disconnected" },
  not_configured: { tone: "outline", label: "Not configured" },
  disabled: { tone: "secondary", label: "Disabled" },
  fatal: { tone: "destructive", label: "Error" },
};

function stateBadge(state: string) {
  return STATE_BADGE[state] ?? { tone: "outline" as const, label: state };
}

export default function ChannelsPage() {
  const [platforms, setPlatforms] = useState<MessagingPlatform[]>([]);
  const [envPath, setEnvPath] = useState("~/.aria/.env");
  const [gatewayStartCommand, setGatewayStartCommand] = useState(
    "aria gateway start",
  );
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const [testingId, setTestingId] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);

  const gatewayRunning = platforms.length > 0 && platforms[0].gateway_running;

  const load = useCallback(() => {
    return api
      .getMessagingPlatforms()
      .then((res) => {
        setPlatforms(res.platforms);
        setEnvPath(res.env_path || "~/.aria/.env");
        setGatewayStartCommand(res.gateway_start_command || "aria gateway start");
      })
      .catch((e) => showToast(`Error: ${e}`, "error"));
  }, [showToast]);

  useEffect(() => {
    load().finally(() => setLoading(false));
  }, [load]);

  const handleTest = async (platform: MessagingPlatform) => {
    setTestingId(platform.id);
    try {
      const res = await api.testMessagingPlatform(platform.id);
      showToast(`${platform.name}: ${res.message}`, res.ok ? "success" : "error");
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setTestingId(null);
    }
  };

  const handleRestart = async () => {
    setRestarting(true);
    try {
      await api.restartGateway();
      showToast("Gateway restarting…", "success");
      // Give the gateway a moment to come up, then refresh status.
      setTimeout(() => void load(), 4000);
    } catch (e) {
      showToast(`Failed to restart: ${e}`, "error");
    } finally {
      setRestarting(false);
    }
  };

  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={handleRestart}
        disabled={restarting}
        prefix={restarting ? <Spinner /> : <RotateCw className="h-4 w-4" />}
      >
        {restarting ? "Restarting…" : "Restart gateway"}
      </Button>,
    );
    return () => setEnd(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setEnd, restarting]);

  const configured = useMemo(
    () => platforms.filter((p) => p.configured).length,
    [platforms],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      {!gatewayRunning && (
        <Card className="border-border">
          <CardContent className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
            <WifiOff className="h-4 w-4 shrink-0" />
            <span>
              The gateway is not running. Configure channels here, then start the
              gateway with <code className="font-courier">{gatewayStartCommand}</code>{" "}
              (or the Restart button above).
            </span>
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        {configured} of {platforms.length} channels configured. Credentials are
        written to <code className="font-courier">{envPath}</code>; the
        gateway connects each enabled channel on its next restart.
      </p>

      {/* Platform list */}
      <div className="grid gap-3">
        {platforms.map((platform) => {
          const badge = stateBadge(platform.state);
          const StateIcon =
            platform.state === "connected"
              ? CheckCircle2
              : platform.state === "fatal" || platform.state === "startup_failed"
                ? AlertTriangle
                : Radio;
          return (
            <Card key={platform.id} className="border-border">
              <CardContent className="flex flex-col gap-4 p-4">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3 min-w-0">
                    <StateIcon
                      className={cn(
                        "h-5 w-5 shrink-0 mt-0.5",
                        platform.state === "connected"
                          ? "text-success"
                          : platform.state === "fatal" ||
                              platform.state === "startup_failed"
                            ? "text-destructive"
                            : "text-muted-foreground",
                      )}
                    />
                    <div className="flex flex-col gap-0.5 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-brand normal-case text-sm font-medium">
                          {platform.name}
                        </span>
                        <Badge tone={badge.tone}>{badge.label}</Badge>
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {platform.description}
                      </span>
                      {platform.error_message && (
                        <span className="text-xs text-destructive">
                          {platform.error_message}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0 self-start sm:self-center">
                    <Button
                      ghost
                      size="sm"
                      onClick={() => handleTest(platform)}
                      disabled={testingId === platform.id}
                      prefix={
                        testingId === platform.id ? (
                          <Spinner />
                        ) : (
                          <PlugZap className="h-4 w-4" />
                        )
                      }
                    >
                      Test
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
