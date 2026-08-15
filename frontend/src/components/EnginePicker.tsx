import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { Engine, SiteDetail } from "../lib/api";
import { ApiError, endpoints } from "../lib/api";
import { Alert, PanelHeader, Spinner, useCollapsible } from "./ui";

/**
 * Choosing how a site gets captured.
 *
 * The form below the picker is generated from the engine's own JSON Schema, so
 * an addon author writes no frontend code — and so this component knows
 * nothing about wget's flags or browsertrix's behaviors.
 *
 * The part worth keeping is what it refuses to hide. An engine declares what
 * it can do, and where a site needs something the engine cannot do the picker
 * says so *before* the capture rather than after: browsertrix genuinely cannot
 * use a cookie jar, and a gated site captured with it archives several
 * thousand copies of the content warning with nothing anywhere explaining why.
 */
export function EnginePicker({ site }: { site: SiteDetail }) {
  const client = useQueryClient();
  const [chosen, setChosen] = useState(site.engine_id);
  const [config, setConfig] = useState<Record<string, unknown>>(site.engine_config);
  const [saved, setSaved] = useState(false);
  // Open by default: it is where the capability warnings live, and this panel
  // exists to say something *before* a capture rather than after. Collapsing
  // it is a choice somebody makes, not one made for them.
  const { open, toggle } = useCollapsible("engine", true);

  const engines = useQuery({ queryKey: ["engines"], queryFn: endpoints.engines });
  const schema = useQuery({
    queryKey: ["engine-schema", chosen],
    queryFn: () => endpoints.engineSchema(chosen),
    enabled: Boolean(chosen),
  });

  // Switching engine throws away the old config: the two schemas share no
  // properties, and `additionalProperties: false` means keeping them would
  // make every save fail validation.
  useEffect(() => {
    if (chosen === site.engine_id) setConfig(site.engine_config);
    else if (schema.data) setConfig({ ...schema.data.defaults });
  }, [chosen, schema.data, site.engine_id, site.engine_config]);

  const save = useMutation({
    mutationFn: () => endpoints.updateSite(site.id, { engine_id: chosen, engine_config: config }),
    onSuccess: async () => {
      setSaved(true);
      await client.invalidateQueries({ queryKey: ["site", site.id] });
    },
  });

  const list = engines.data ?? [];
  const engine = list.find((e) => e.id === chosen);
  const dirty = chosen !== site.engine_id || JSON.stringify(config) !== JSON.stringify(site.engine_config);

  return (
    <section className="card p-5">
      <PanelHeader
        title="Capture engine"
        hint="How this site is fetched. Existing captures are unaffected."
        open={open}
        onToggle={toggle}
      />

      {open && <div className="mt-4 space-y-4">
        <div className="grid gap-2 sm:grid-cols-2">
          {list
            .filter((e) => e.enabled)
            .map((option) => (
              <EngineOption
                key={option.id}
                engine={option}
                selected={option.id === chosen}
                onSelect={() => {
                  setChosen(option.id);
                  setSaved(false);
                }}
              />
            ))}
        </div>

        {list.some((e) => !e.enabled) && (
          <Alert kind="warn" title="An engine failed to load">
            <ul className="space-y-1">
              {list
                .filter((e) => !e.enabled)
                .map((e) => (
                  <li key={e.id}>
                    <code>{e.id}</code>: {e.error}
                  </li>
                ))}
            </ul>
          </Alert>
        )}

        {engine && <CapabilityNotes engine={engine} site={site} />}

        {schema.data && (
          <ConfigForm
            schema={schema.data.schema}
            value={config}
            onChange={(next) => {
              setConfig(next);
              setSaved(false);
            }}
          />
        )}

        <div className="flex items-center gap-3">
          <button
            className="btn-primary"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner />}
            Save
          </button>
          {saved && !dirty && <span className="text-sm text-ok">Saved.</span>}
        </div>
        {save.error && (
          <Alert kind="error">
            {(save.error as ApiError).message}
            {(save.error as ApiError).problems.map((problem) => (
              <div key={problem} className="mt-1 font-mono text-xs">
                {problem}
              </div>
            ))}
          </Alert>
        )}
      </div>}
    </section>
  );
}

function EngineOption({
  engine,
  selected,
  onSelect,
}: {
  engine: Engine;
  selected: boolean;
  onSelect: () => void;
}) {
  const caps = engine.capabilities as Record<string, unknown>;
  return (
    <button
      className={`rounded-md border p-3 text-left ${
        selected ? "border-accent bg-accent/5" : "border-border hover:bg-raised"
      } ${engine.available ? "" : "opacity-60"}`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{engine.name}</span>
        <span className="text-[10px] uppercase text-muted">{engine.version}</span>
        {caps.javascript ? (
          <span className="rounded bg-raised px-1.5 py-0.5 text-[10px] text-muted">JS</span>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-muted">{engine.description}</p>
      {!engine.available && (
        <p className="mt-1.5 text-xs text-warn">{engine.unavailable_reason}</p>
      )}
    </button>
  );
}

/** What this engine cannot do for *this* site. Said before the capture. */
function CapabilityNotes({ engine, site }: { engine: Engine; site: SiteDetail }) {
  const caps = engine.capabilities as Record<string, unknown>;
  const auth = (caps.auth as string[] | undefined) ?? [];
  const notes: string[] = [];

  // A browser profile is the other way past a gate, and the only one this
  // engine has. Warning about the cookie jar while one is attached would be
  // telling somebody a solved problem is unsolved.
  // Both halves have to line up: the engine must accept a kind of credential
  // *and* the profile must actually hold that kind. Checking only the engine
  // meant a profile carrying a browsertrix tarball and no jar looked fine to
  // wget, which reads neither — reported as a second blog captured signed out
  // and stuck at the interstitial, with nothing anywhere saying why.
  const passesTheGate =
    (auth.includes("cookies") && site.profile_has_cookies) ||
    (auth.includes("browser_profile") && site.profile_has_browser_profile);

  if (site.profile_id && !passesTheGate) {
    const gap =
      auth.includes("cookies") && site.profile_has_browser_profile
        ? // The engine reads jars, the profile holds a tarball. Both are
          // fine on their own and together they are nothing.
          `${engine.name} reads a cookie jar, and this site's access profile holds a ` +
          "browsertrix browser profile and no jar. This capture will run signed out. " +
          "Switch this site to the browsertrix engine, or upload a cookies.txt to that " +
          "profile."
        : auth.includes("cookies")
          ? `${engine.name} reads a cookie jar, and this site's access profile has no ` +
            "cookies stored. This capture will run signed out."
          : auth.includes("browser_profile")
            ? `This site has an access profile, and ${engine.name} cannot use a cookie jar — ` +
              "it has no cookie option at all. Attach a browsertrix browser profile to that " +
              "access profile, or anything behind the gate will be archived as the gate."
            : `This site has an access profile, and ${engine.name} cannot use a cookie jar. ` +
              "Anything behind the gate will be archived as the gate — use an engine that " +
              "declares cookie support, or check the capture afterwards.";
    notes.push(gap);
  }
  if (!caps.javascript) {
    notes.push(
      "This engine does not run page JavaScript, so content built by script — " +
        "galleries, lazy-loaded images, links added after load — will not be found.",
    );
  }
  if (caps.incremental === false) {
    notes.push(
      "No incremental capture: every run re-stores everything, so feed captures " +
        "cost the same as full ones.",
    );
  }

  if (!notes.length) return null;
  return (
    <Alert kind="warn" title="Worth knowing about this engine">
      <ul className="list-disc space-y-1 pl-5">
        {notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </Alert>
  );
}

type Property = {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
  pattern?: string;
};

/**
 * A form from a JSON Schema.
 *
 * Deliberately small: `type`, `title`, `description`, `default`, `enum` and
 * the numeric bounds, which is what docs/05 promises an addon author. The
 * server validates against the same schema regardless, so anything this
 * cannot express degrades to a text box rather than to a hole.
 */
function ConfigForm({
  schema,
  value,
  onChange,
}: {
  schema: Record<string, unknown>;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const properties = (schema.properties ?? {}) as Record<string, Property>;
  const entries = Object.entries(properties);
  if (!entries.length) return null;

  const set = (key: string, next: unknown) => onChange({ ...value, [key]: next });

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {entries.map(([key, property]) => {
        const label = property.title || key;
        const current = value[key] ?? property.default;
        const id = `engine-${key}`;

        if (property.type === "boolean") {
          return (
            <div key={key} className="sm:col-span-2">
              <label className="flex items-center gap-2 text-sm" htmlFor={id}>
                <input
                  id={id}
                  type="checkbox"
                  checked={Boolean(current)}
                  onChange={(e) => set(key, e.target.checked)}
                />
                {label}
              </label>
              {property.description && <p className="hint mt-0.5 ml-6">{property.description}</p>}
            </div>
          );
        }

        return (
          <div key={key}>
            <label className="label" htmlFor={id}>
              {label}
            </label>
            {property.enum ? (
              <select
                id={id}
                className="field"
                value={String(current ?? "")}
                onChange={(e) => set(key, e.target.value)}
              >
                {property.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                className="field"
                type={property.type === "integer" || property.type === "number" ? "number" : "text"}
                value={String(current ?? "")}
                min={property.minimum}
                max={property.maximum}
                onChange={(e) =>
                  set(
                    key,
                    property.type === "integer"
                      ? Number.parseInt(e.target.value || "0", 10)
                      : property.type === "number"
                        ? Number(e.target.value || 0)
                        : e.target.value,
                  )
                }
              />
            )}
            {property.description && <p className="hint mt-1">{property.description}</p>}
          </div>
        );
      })}
    </div>
  );
}
