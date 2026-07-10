require "yaml"

# Keep Jekyll branding in sync with the central Paperpulse configuration.
Jekyll::Hooks.register :site, :after_init do |site|
  config_path = File.join(site.source, "_data", "paperpulse.yml")
  unless File.file?(config_path)
    Jekyll.logger.warn "Paperpulse:", "#{config_path} is missing; site branding is unset"
    next
  end

  paperpulse = YAML.safe_load(File.read(config_path), aliases: true) || {}
  blog = paperpulse.fetch("blog", {})
  %w[title tagline description].each do |key|
    site.config[key] = blog[key] if blog.key?(key)
  end
end
