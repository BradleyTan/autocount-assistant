/*
  Creates the read-only login used by autocount-assist.
  Run this yourself in SQL Server Management Studio as an administrator.
  autocount-assist never runs this script.

  1. Replace <STRONG_PASSWORD> below (do not save the real password in this file or in git).
  2. Replace AED_YOURBOOK with each account book database the tool may read, repeating
     the USE block once per database.
  3. Set the password on your PC only:
       setx AC_SQL_USER ac_readonly
       setx AC_SQL_PASSWORD "<the same password>"
     Or skip SQL logins entirely: grant your Windows account db_datareader instead and leave
     AC_SQL_USER unset, and the tool will use Windows authentication.
*/
USE [master];
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'ac_readonly')
    CREATE LOGIN [ac_readonly] WITH PASSWORD = N'<STRONG_PASSWORD>', CHECK_POLICY = ON, DEFAULT_DATABASE = [master];
GO

-- Allows `autocount-assist dbs` to list account books (sys.databases is visible to all logins by default).

USE [AED_YOURBOOK];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'ac_readonly')
    CREATE USER [ac_readonly] FOR LOGIN [ac_readonly];
ALTER ROLE [db_datareader] ADD MEMBER [ac_readonly];
-- Explicitly deny writes, even if someone later adds the user to another role.
ALTER ROLE [db_denydatawriter] ADD MEMBER [ac_readonly];
GO
