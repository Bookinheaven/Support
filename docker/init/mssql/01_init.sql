-- Microsoft SQL Server Sample Development Schema

IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'support_dev')
BEGIN
    CREATE DATABASE support_dev;
END
GO

USE support_dev;
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'organizations')
BEGIN
    CREATE TABLE organizations (
        id INT IDENTITY(1,1) PRIMARY KEY,
        name NVARCHAR(100) NOT NULL UNIQUE,
        created_at DATETIME2 DEFAULT CURRENT_TIMESTAMP
    );
END
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'users')
BEGIN
    CREATE TABLE users (
        id INT IDENTITY(1,1) PRIMARY KEY,
        org_id INT NOT NULL,
        email NVARCHAR(255) NOT NULL UNIQUE,
        full_name NVARCHAR(100) NOT NULL,
        created_at DATETIME2 DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_user_org FOREIGN KEY (org_id)
            REFERENCES organizations(id) ON DELETE CASCADE
    );
END
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'orders')
BEGIN
    CREATE TABLE orders (
        id INT IDENTITY(1,1) PRIMARY KEY,
        user_id INT NOT NULL,
        order_code NVARCHAR(50) NOT NULL UNIQUE,
        total_amount DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
        status NVARCHAR(30) DEFAULT 'completed',
        created_at DATETIME2 DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_order_user FOREIGN KEY (user_id)
            REFERENCES users(id) ON DELETE CASCADE
    );
END
GO

IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'order_items')
BEGIN
    CREATE TABLE order_items (
        id INT IDENTITY(1,1) PRIMARY KEY,
        order_id INT NOT NULL,
        product_name NVARCHAR(150) NOT NULL,
        quantity INT NOT NULL DEFAULT 1,
        unit_price DECIMAL(10, 2) NOT NULL,
        CONSTRAINT fk_item_order FOREIGN KEY (order_id)
            REFERENCES orders(id) ON DELETE CASCADE
    );
END
GO

-- Seed initial records if empty
IF NOT EXISTS (SELECT 1 FROM organizations)
BEGIN
    SET IDENTITY_INSERT organizations ON;
    INSERT INTO organizations (id, name) VALUES (1, 'Acme Corp'), (2, 'Globex');
    SET IDENTITY_INSERT organizations OFF;

    SET IDENTITY_INSERT users ON;
    INSERT INTO users (id, org_id, email, full_name) VALUES 
        (1, 1, 'alice@acme.com', 'Alice Smith'),
        (2, 1, 'bob@acme.com', 'Bob Jones'),
        (3, 2, 'charlie@globex.com', 'Charlie Brown');
    SET IDENTITY_INSERT users OFF;

    SET IDENTITY_INSERT orders ON;
    INSERT INTO orders (id, user_id, order_code, total_amount, status) VALUES
        (1, 1, 'ORD-2026-001', 199.99, 'completed'),
        (2, 2, 'ORD-2026-002', 49.50, 'processing');
    SET IDENTITY_INSERT orders OFF;

    SET IDENTITY_INSERT order_items ON;
    INSERT INTO order_items (id, order_id, product_name, quantity, unit_price) VALUES
        (1, 1, 'Wireless Keyboard', 1, 99.99),
        (2, 1, 'Precision Mouse', 1, 100.00),
        (3, 2, 'USB-C Cable', 2, 24.75);
    SET IDENTITY_INSERT order_items OFF;
END
GO
