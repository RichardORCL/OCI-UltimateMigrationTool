<#
.SYNOPSIS
  Package the helper Terraform as a Resource Manager stack zip and optionally create the stack.
.EXAMPLE
  .\package_stack.ps1                                   # -> <repo root>\oci-ultimate-migration-tool-stack.zip
                                                        #    (committed; the README "Deploy to Oracle Cloud" button links to it)
  .\package_stack.ps1 -CreateInCompartment ocid1.compartment.oc1..aaaa -Name OCI-Ultimate-Migration-Tool
#>
param(
    [string]$CreateInCompartment = "",
    [string]$Name = "OCI-Ultimate-Migration-Tool"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $here "terraform"
$root = (Resolve-Path (Join-Path $here "..\..")).Path
$zip = Join-Path $root "oci-ultimate-migration-tool-stack.zip"

if (Test-Path $zip) { Remove-Item $zip }
$files = @("main.tf", "variables.tf", "outputs.tf", "schema.yaml", "cloud-init.yaml") | ForEach-Object { Join-Path $src $_ }
Compress-Archive -Path $files -DestinationPath $zip
Write-Host "wrote $zip"

if ($CreateInCompartment) {
    $id = oci resource-manager stack create `
        --compartment-id $CreateInCompartment `
        --display-name $Name `
        --description "OCI Ultimate Migration Tool VM" `
        --config-source $zip `
        --terraform-version "1.5.x" `
        --query 'data.id' --raw-output
    Write-Host "Stack created: $id"
    Write-Host "Set the variables and run a plan/apply job in the console, or:"
    Write-Host "  oci resource-manager job create-apply-job --stack-id $id --execution-plan-strategy AUTO_APPROVED"
}
