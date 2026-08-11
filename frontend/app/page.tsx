"use client";

import { useCallback, useEffect, useState } from "react";

type Health = {
  status: string;
  service: string;
  version: string;
};

type Hello = {
  message: string;
};

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [hello, setHello] = useState<Hello | null>(null);
  const [name, setName] = useState("world");
  const [error, setError] = useState<string | null>(null);

  const callHello = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch(`/api/hello?name=${encodeURIComponent(name)}`);
      if (!res.ok) throw new Error(`Backend responded with ${res.status}`);
      setHello(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [name]);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/health");
        if (!res.ok) throw new Error(`Backend responded with ${res.status}`);
        setHealth(await res.json());
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, []);

  return (
    <main style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 1rem", fontFamily: "system-ui, sans-serif", lineHeight: 1.6 }}>
      <h1>Next.js + FastAPI on Vercel</h1>
      <p>
        This page is served by the <strong>Next.js</strong> frontend service. The calls below hit the{" "}
        <strong>Python FastAPI</strong> backend service.
      </p>

      <section style={{ marginTop: "1.5rem", padding: "1rem", border: "1px solid #ddd", borderRadius: 8 }}>
        <h2 style={{ marginTop: 0 }}>Backend health</h2>
        {health ? (
          <pre>{JSON.stringify(health, null, 2)}</pre>
        ) : error ? null : (
          <p>Loading&hellip;</p>
        )}
      </section>

      <section style={{ marginTop: "1.5rem", padding: "1rem", border: "1px solid #ddd", borderRadius: 8 }}>
        <h2 style={{ marginTop: 0 }}>Say hello from the backend</h2>
        <label htmlFor="name">Your name: </label>
        <input
          id="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ marginRight: "0.5rem", padding: "0.25rem 0.5rem" }}
        />
        <button onClick={callHello} style={{ padding: "0.25rem 0.75rem" }}>
          Call /api/hello
        </button>
        {hello ? <pre>{JSON.stringify(hello, null, 2)}</pre> : null}
      </section>

      {error ? (
        <p style={{ color: "#b00020" }}>
          Backend unreachable: {error}
        </p>
      ) : null}
    </main>
  );
}
