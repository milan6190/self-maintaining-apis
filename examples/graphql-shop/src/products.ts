import { gql } from "@apollo/client";

// Written against Shop API v1.
export const LIST_PRODUCTS = gql`
  query ListProducts($n: Int) {
    products(pageSize: $n) {
      id
      title
      price
      legacySku
    }
  }
`;

export function formatProduct(p: { title: string; price: number }) {
  return `${p.title}: $${p.price.toFixed(2)}`;
}
