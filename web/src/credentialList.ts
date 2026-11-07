import { request } from "./api";

/** One row of GET /api/auth/credentials: a web session or an API key. */
export type Credential = {
  id: string;
  kind: string;
  label: string;
  created_at: number;
  expires_at: number | null;
  revoked_at: number | null;
};
export async function loadCredentials(signal?: AbortSignal) {
  const data = await request<{ credentials: Credential[] }>(
    "/api/auth/credentials",
    { signal },
  );
  return data.credentials;
}
export const revokeCredential = (id: string) =>
  request(`/api/auth/credentials/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
