import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Alert, EmptyState, Field, Spinner } from "../components/ui";
import type { Folder, MoveOutcome } from "../lib/api";
import { ApiError, endpoints } from "../lib/api";
import { bytes } from "../lib/format";

/**
 * The folder tree, which is also the directory tree under /data/archives.
 *
 * Drag and drop is the fast path, not the only one: dragging is unusable with
 * a keyboard and awkward on a phone, so every folder also carries a "Move to"
 * select that does the identical thing. Anything reachable only by dragging
 * would be unreachable for some people entirely.
 */
export default function Folders() {
  const client = useQueryClient();
  const folders = useQuery({ queryKey: ["folders"], queryFn: endpoints.folders });
  const [creatingIn, setCreatingIn] = useState<number | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const flat = flatten(folders.data ?? []);

  const refresh = async () => {
    await client.invalidateQueries({ queryKey: ["folders"] });
    await client.invalidateQueries({ queryKey: ["sites"] });
  };

  const announce = (outcome: MoveOutcome) => {
    setError(null);
    setNotice(
      outcome.status === "queued"
        ? "That folder is on a different filesystem, so the archives are being copied " +
            "rather than moved. Watch it finish under Jobs."
        : null,
    );
  };

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Folders</h1>
          <p className="mt-1 text-sm text-muted">
            This tree is the directory tree under <code className="font-mono">/data/archives</code>,
            so it is the same structure over SMB.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setCreatingIn(null)}>
          New top-level folder
        </button>
      </header>

      {error && <Alert kind="error">{error}</Alert>}
      {notice && <Alert kind="info">{notice}</Alert>}

      {creatingIn !== undefined && (
        <CreateFolder
          parentId={creatingIn}
          parentName={flat.find((f) => f.id === creatingIn)?.path}
          onDone={async () => {
            setCreatingIn(undefined);
            await refresh();
          }}
          onCancel={() => setCreatingIn(undefined)}
        />
      )}

      {folders.isLoading ? (
        <Spinner className="h-5 w-5 text-muted" />
      ) : folders.data?.length ? (
        <div className="card divide-y divide-border">
          {folders.data.map((node) => (
            <FolderRow
              key={node.id}
              node={node}
              depth={0}
              all={flat}
              onAddChild={setCreatingIn}
              onChanged={refresh}
              onOutcome={announce}
              onError={setError}
            />
          ))}
        </div>
      ) : (
        <EmptyState title="No folders" />
      )}
    </div>
  );
}

type Flat = { id: number; path: string; depth: number };

function flatten(nodes: Folder[], depth = 0): Flat[] {
  return nodes.flatMap((node) => [
    { id: node.id, path: node.path, depth },
    ...flatten(node.children, depth + 1),
  ]);
}

function FolderRow({
  node,
  depth,
  all,
  onAddChild,
  onChanged,
  onOutcome,
  onError,
}: {
  node: Folder;
  depth: number;
  all: Flat[];
  onAddChild: (id: number) => void;
  onChanged: () => Promise<void>;
  onOutcome: (outcome: MoveOutcome) => void;
  onError: (message: string) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(node.name);
  const [dragOver, setDragOver] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const run = async (fn: () => Promise<MoveOutcome | { ok: boolean }>) => {
    try {
      const outcome = await fn();
      if ("status" in outcome) onOutcome(outcome);
      await onChanged();
    } catch (err) {
      onError((err as ApiError).message);
    }
  };

  // A folder cannot be moved into itself or into anything below it — the
  // server refuses either way, but offering the option is a trap.
  const targets = all.filter(
    (f) => f.id !== node.id && !f.path.startsWith(`${node.path}/`) && f.id !== node.parent_id,
  );

  return (
    <>
      <div
        className={`flex flex-wrap items-center gap-3 px-4 py-3 ${dragOver ? "bg-accent/10" : ""}`}
        style={{ paddingLeft: `${depth * 1.5 + 1}rem` }}
        draggable
        onDragStart={(event) => {
          event.dataTransfer.setData("application/x-cairn-folder", String(node.id));
          event.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("application/x-cairn-folder")) {
            event.preventDefault();
            setDragOver(true);
          }
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragOver(false);
          const moved = Number(event.dataTransfer.getData("application/x-cairn-folder"));
          if (moved && moved !== node.id) {
            void run(() => endpoints.reparentFolder(moved, node.id));
          }
        }}
      >
        <div className="min-w-0 flex-1">
          {renaming ? (
            <form
              className="flex gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                setRenaming(false);
                if (name !== node.name) void run(() => endpoints.renameFolder(node.id, name));
              }}
            >
              <input
                className="field max-w-xs"
                value={name}
                onChange={(event) => setName(event.target.value)}
                autoFocus
              />
              <button className="btn-primary">Rename</button>
              <button type="button" className="btn-ghost" onClick={() => setRenaming(false)}>
                Cancel
              </button>
            </form>
          ) : (
            <>
              <span className="font-medium">{node.name}</span>
              <span className="ml-2 font-mono text-xs text-muted">{node.path}</span>
            </>
          )}
        </div>

        <dl className="flex shrink-0 gap-5 text-right text-xs">
          <div>
            <dt className="text-muted">Sites</dt>
            <dd className="tabular-nums">
              {node.site_count}
              {node.total_site_count !== node.site_count && (
                <span className="text-muted"> / {node.total_site_count}</span>
              )}
            </dd>
          </div>
          <div className="w-16">
            <dt className="text-muted">Size</dt>
            <dd className="tabular-nums">{bytes(node.total_size_bytes)}</dd>
          </div>
        </dl>

        <div className="flex shrink-0 items-center gap-1.5">
          <Link className="btn-ghost text-xs" to={`/sites?folder_id=${node.id}`}>
            Open
          </Link>
          <select
            className="field w-28 py-1 text-xs"
            value=""
            aria-label={`Move ${node.name} to another folder`}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "") return;
              void run(() =>
                endpoints.reparentFolder(node.id, value === "root" ? null : Number(value)),
              );
            }}
          >
            <option value="">Move to…</option>
            {node.parent_id !== null && <option value="root">Top level</option>}
            {targets.map((f) => (
              <option key={f.id} value={f.id}>
                {"— ".repeat(f.depth)}
                {f.path}
              </option>
            ))}
          </select>
          <button className="btn-ghost text-xs" onClick={() => onAddChild(node.id)}>
            Add
          </button>
          <button className="btn-ghost text-xs" onClick={() => setRenaming(true)}>
            Rename
          </button>
          <button
            className="btn-ghost text-xs text-danger"
            onClick={() => setConfirming(true)}
          >
            Delete
          </button>
        </div>
      </div>

      {confirming && (
        <DeleteFolder
          node={node}
          targets={targets}
          onCancel={() => setConfirming(false)}
          onDone={async () => {
            setConfirming(false);
            await onChanged();
          }}
          onError={onError}
        />
      )}

      {node.children.map((child) => (
        <FolderRow
          key={child.id}
          node={child}
          depth={depth + 1}
          all={all}
          onAddChild={onAddChild}
          onChanged={onChanged}
          onOutcome={onOutcome}
          onError={onError}
        />
      ))}
    </>
  );
}

function DeleteFolder({
  node,
  targets,
  onCancel,
  onDone,
  onError,
}: {
  node: Folder;
  targets: Flat[];
  onCancel: () => void;
  onDone: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [reassign, setReassign] = useState<string>("");

  return (
    <div className="border-l-2 border-danger bg-danger/5 px-4 py-3 text-sm">
      <p className="font-medium">Delete {node.path}?</p>
      <p className="mt-1 text-muted">
        The archives are never deleted with a folder. If it holds any, say where they should
        go — including any already in the trash, which still belong to it.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          className="field w-64"
          value={reassign}
          onChange={(event) => setReassign(event.target.value)}
        >
          <option value="">Only if it is empty</option>
          {targets.map((f) => (
            <option key={f.id} value={f.id}>
              Move its sites to {f.path}
            </option>
          ))}
        </select>
        <button
          className="btn-danger"
          onClick={async () => {
            try {
              await endpoints.deleteFolder(node.id, reassign ? Number(reassign) : undefined);
              await onDone();
            } catch (err) {
              onError((err as ApiError).message);
              onCancel();
            }
          }}
        >
          Delete folder
        </button>
        <button className="btn-ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function CreateFolder({
  parentId,
  parentName,
  onDone,
  onCancel,
}: {
  parentId: number | null;
  parentName?: string;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => endpoints.createFolder({ name, parent_id: parentId }),
    onSuccess: onDone,
  });

  return (
    <form
      className="card space-y-4 p-5"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate();
      }}
    >
      {create.error && <Alert kind="error">{(create.error as ApiError).message}</Alert>}
      <Field
        label={parentName ? `New folder inside ${parentName}` : "New top-level folder"}
        htmlFor="folder-name"
        hint="This becomes a real directory. Capitals and spaces are kept; characters a
              Windows share cannot carry are not."
      >
        <input
          id="folder-name"
          className="field max-w-sm"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Photography"
          autoFocus
          required
        />
      </Field>
      <div className="flex gap-2">
        <button className="btn-primary" disabled={create.isPending || !name.trim()}>
          {create.isPending && <Spinner />}
          Create
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
