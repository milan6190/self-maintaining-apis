import axios from "axios";

// Orders REST API (see specs/orders-api.yaml)
export const API_BASE = "https://api.acme.com/v2";

export async function listOrders() {
  const { data } = await axios.get(`${API_BASE}/orders`);
  return data;
}

export async function cancelOrder(id: string) {
  return axios.post(`${API_BASE}/orders/${id}/cancel`);
}

export const graphqlClient = { uri: "https://api.acme.com/graphql" };
