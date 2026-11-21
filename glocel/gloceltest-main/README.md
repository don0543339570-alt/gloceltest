# GLOCEL

Inventory, sales and customer management for an imported-parts business (Flask + Neon Postgres, deployed on Vercel).

- **Sign-in & users** – admin/staff roles, user management, password change.
- **Parts & stock** – landed-cost pricing (USD costs × exchange rate × margin), restock, low-stock/reorder list.
- **Sales** – cart-based new sale, barcode/QR scanning from a phone, printable invoices, WhatsApp share, paid/unpaid tracking.
- **Customers** – built automatically from invoices: purchase history, total spent, money owed.
- **Mobile-first layout** – bottom tab bar on phones; can be added to the home screen.

Setup and deployment (Neon database + Vercel hosting): see [`DEPLOY.md`](DEPLOY.md).
