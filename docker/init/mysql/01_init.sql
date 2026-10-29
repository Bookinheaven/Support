-- MySQL Sample Development Schema

CREATE TABLE IF NOT EXISTS organizations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    org_id INT NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_org FOREIGN KEY (org_id)
        REFERENCES organizations(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    order_code VARCHAR(50) NOT NULL UNIQUE,
    total_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    status VARCHAR(30) DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_order_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    product_name VARCHAR(150) NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(10, 2) NOT NULL,
    CONSTRAINT fk_item_order FOREIGN KEY (order_id)
        REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Seed initial records
INSERT IGNORE INTO organizations (id, name) VALUES (1, 'Acme Corp'), (2, 'Globex');
INSERT IGNORE INTO users (id, org_id, email, full_name) VALUES 
    (1, 1, 'alice@acme.com', 'Alice Smith'),
    (2, 1, 'bob@acme.com', 'Bob Jones'),
    (3, 2, 'charlie@globex.com', 'Charlie Brown');
INSERT IGNORE INTO orders (id, user_id, order_code, total_amount, status) VALUES
    (1, 1, 'ORD-2026-001', 199.99, 'completed'),
    (2, 2, 'ORD-2026-002', 49.50, 'processing');
INSERT IGNORE INTO order_items (id, order_id, product_name, quantity, unit_price) VALUES
    (1, 1, 'Wireless Keyboard', 1, 99.99),
    (2, 1, 'Precision Mouse', 1, 100.00),
    (3, 2, 'USB-C Cable', 2, 24.75);
