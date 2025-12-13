# Box Connector

This connector integrates Onyx with [Box](https://www.box.com/), a cloud content management platform. The connector allows Onyx to index and search through documents stored in Box.

## Features

- Indexes files stored in Box
- Supports recursive folder traversal
- Handles file updates based on modification time
- Performs token refresh for long-term authentication
- Implements pruning support via SlimConnector interface

## Authentication

The Box connector uses OAuth 2.0 for authentication. You'll need to create a Box application in the Box Developer Console to get the required credentials.

### Creating a Box Application

1. Go to the [Box Developer Console](https://app.box.com/developers/console)
2. Click "Create New App"
3. Select "Custom App" as the app type
4. Choose "Server Authentication (with JWT)" as the authentication method
5. Name your app and click "Create App"
6. In the configuration screen:
   - Under "Application Access", select "App + Enterprise Access"
   - Under "Application Scopes", select the following scopes:
     - Read and write all files and folders stored in Box
     - Manage users and groups
   - Under "Advanced Features", enable "Make API calls using the as-user header"
7. Click "Save Changes"

### Getting OAuth Credentials

1. In your app's configuration page, navigate to the "OAuth 2.0 Credentials" section
2. Note down the "Client ID" and "Client Secret"
3. Generate an access token by clicking "Generate Token" (or follow Box's OAuth flow documentation for server-side applications)

## Configuration

When setting up the Box connector in Onyx, you'll need to provide:

1. **Client ID**: Your Box application client ID
2. **Client Secret**: Your Box application client secret  
3. **Access Token**: An OAuth access token with appropriate permissions
4. **Refresh Token** (optional but recommended): A refresh token for automatic token renewal

## Permissions Required

The Box connector requires the following permissions:

- Read access to all files and folders
- Permission to download file content
- User read permissions (to get file owner information)

## Limitations

- File size limits apply based on Onyx configuration
- Some file types may not be properly indexed (executables, videos, audio files)
- Box API rate limits may impact performance for very large Box instances

## Troubleshooting

Common issues:

1. **Authentication failures**: Verify that your OAuth credentials are correct and not expired
2. **Permission errors**: Ensure your Box application has the necessary scopes and access levels
3. **Rate limiting**: If you're indexing a large Box instance, you may hit API rate limits - consider increasing batch sizes or implementing backoff strategies

For more details on the Box API used by this connector, refer to the [Box API Documentation](https://developer.box.com/reference/).