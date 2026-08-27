// TreeSetupWizard.cs
// Unity 2017-compatible Editor tool (C# 4.0: no string interpolation, no
// expression-bodied members, no null-conditional operators). Pure
// editor-time setup utility -- not a MonoBehaviour.
//
// IMPORTANT: this script must live inside a folder literally named "Editor"
// somewhere under Assets (e.g. Assets/Editor/TreeSetupWizard.cs).
//
// Open it via: Tools > Terrain > Tree Setup Wizard
//
// This is a generalization of an earlier single-purpose "OakTreeSetupWizard"
// so the same tool now supports TWO naming conventions:
//
//   1. "Oak Seasonal" (the original pipeline this was built for):
//        Oak.fbx, Oak_Bark_Albedo_<Season>.png, Oak_Bark_Normal_<Season>.png,
//        Oak_Bark_Roughness_<Season>.png, Oak_Bark_KnotMask.png,
//        Oak_Foliage_Albedo_<Season>.png, Oak_Foliage_Normal_<Season>.png,
//        Oak_Billboard.png -- expects Custom/OakBarkKnotVariant and
//        Custom/OakFoliageWind shaders already in the project, and expects
//        the FBX to have separate named children Oak_LOD0/1/2/3, each with
//        its own Renderer.
//
//   2. "Procedural Conifer" (this project's Blender pipeline --
//        1_generate_branch_foliage.py / 2_generate_foliage_shader.py /
//        3_assemble_tree_and_export.py):
//        Tree_00.fbx, Tree_01.fbx, ... (any number), bark_albedo.png,
//        bark_normal.png, branch_00_card.png, branch_00_card_normal.png,
//        branch_01_card.png, ... -- each FBX is a SINGLE joined mesh with
//        one Renderer holding multiple material slots (one bark slot named
//        "BarkMaterial_<species>", plus one slot per branch card named
//        "BillboardCardMat_<NN>"). No custom shaders required -- everything
//        uses the built-in Standard shader (bark = Opaque, branch cards =
//        Cutout).
//
// The wizard auto-detects which convention applies by scanning the source
// folder (or you can force one from the dropdown), then:
//   1. Copies every .fbx and .png it finds in the source folder into your
//      Assets folder and imports them.
//   2. Fixes texture import settings (Normal Map type, linear vs sRGB) by
//      filename convention.
//   3. For every .fbx found, instantiates it, walks its renderer(s),
//      matches each material SLOT NAME against the resolved convention's
//      texture files, and builds/reuses a properly configured Standard
//      shader material (or a custom shader, for the Oak convention, if
//      present) -- replacing whatever Unity auto-imported.
//   4. Saves each configured instance as its own prefab asset.
//   5. Optionally registers every created prefab as a Tree Prototype on a
//      Terrain you pick, so Paint Trees can use them immediately.
//
// Materials are cached per unique slot name during a run, so e.g. all five
// Tree_00..04.fbx sharing "BillboardCardMat_03" only get ONE material asset
// between them, matching how the Blender pipeline shares baked textures
// across every generated tree.
//
// KNOWN LIMITATION: Blender's FBX exporter does not reliably carry over
// node-based color tints (e.g. red pine's reddish bark, which in Blender is
// a MixRGB node multiplying bark_albedo.png, not a change to the pixels
// themselves). This wizard reapplies that tint on the Unity side via the
// Standard shader's built-in Color property, keyed off the species name
// embedded in the "BarkMaterial_<species>" slot name. If you add or rename
// a species / change its bark_tint in 3_assemble_tree_and_export.py's
// SPECIES_PROFILES, update SpeciesBarkTints below to match.

using UnityEngine;
using UnityEditor;
using System.IO;
using System.Collections.Generic;
using System.Text.RegularExpressions;

public class TreeSetupWizard : EditorWindow
{
    private enum NamingConvention { AutoDetect, OakSeasonal, ProceduralConifer }
    private enum Season { Spring, Summer, Fall }

    private string sourceFolder = "";
    private string importFolder = "Assets/GeneratedTrees";
    private NamingConvention convention = NamingConvention.AutoDetect;
    private Season season = Season.Summer;
    private bool registerOnTerrain = true;
    private Terrain targetTerrain;

    // Populated by "Scan Folder" (recursive) -- lets the user pick which
    // FBX files to actually process instead of blindly processing every
    // one found under the source folder.
    private List<string> scannedFbxPaths = new List<string>();
    private Dictionary<string, bool> fbxSelection = new Dictionary<string, bool>();
    private Dictionary<string, string> scannedPngPaths = new Dictionary<string, string>();  // filename -> full path
    private bool hasScanned = false;
    private string lastScannedFolder = "";
    private Vector2 fbxListScroll;

    // Must match SPECIES_PROFILES[...]["bark_tint"] in
    // 3_assemble_tree_and_export.py -- species not listed here get no tint
    // (plain white, i.e. the baked albedo shows through unmodified).
    private static readonly Dictionary<string, Color> SpeciesBarkTints = new Dictionary<string, Color>()
    {
        { "pine_red", new Color(1.3f, 0.82f, 0.68f) },
    };

    [MenuItem("Tools/Terrain/Tree Setup Wizard")]
    public static void ShowWindow()
    {
        TreeSetupWizard window = GetWindow<TreeSetupWizard>("Tree Setup Wizard");
        window.minSize = new Vector2(420f, 560f);
    }

    private void OnEnable()
    {
        if (targetTerrain == null && Terrain.activeTerrain != null)
        {
            targetTerrain = Terrain.activeTerrain;
        }
    }

    private void OnGUI()
    {
        EditorGUILayout.LabelField("1. Source Folder", EditorStyles.boldLabel);
        EditorGUILayout.HelpBox(
            "The top-level folder to scan RECURSIVELY for .fbx and .png files " +
            "(e.g. a parent folder containing exported_trees/ and " +
            "baked_textures/ as subfolders -- they don't need to be flattened " +
            "into one folder anymore).", MessageType.None);
        EditorGUILayout.BeginHorizontal();
        EditorGUILayout.TextField(sourceFolder);
        if (GUILayout.Button("Browse...", GUILayout.Width(80f)))
        {
            string picked = EditorUtility.OpenFolderPanel("Select tree output folder", sourceFolder, "");
            if (!string.IsNullOrEmpty(picked))
            {
                sourceFolder = picked;
            }
        }
        EditorGUILayout.EndHorizontal();

        if (GUILayout.Button("Scan Folder (recursive)", GUILayout.Height(24f)))
        {
            ScanFolder();
        }

        if (hasScanned)
        {
            if (scannedFbxPaths.Count == 0)
            {
                EditorGUILayout.HelpBox("No .fbx files found under this folder.", MessageType.Warning);
            }
            else
            {
                EditorGUILayout.Space();
                EditorGUILayout.LabelField(
                    "Found " + scannedFbxPaths.Count + " FBX file(s) -- choose which to process:",
                    EditorStyles.boldLabel);

                EditorGUILayout.BeginHorizontal();
                if (GUILayout.Button("Select All"))
                {
                    SetAllFbxSelection(true);
                }
                if (GUILayout.Button("Select None"))
                {
                    SetAllFbxSelection(false);
                }
                EditorGUILayout.EndHorizontal();

                fbxListScroll = EditorGUILayout.BeginScrollView(fbxListScroll, GUILayout.Height(140f));
                for (int i = 0; i < scannedFbxPaths.Count; i++)
                {
                    string path = scannedFbxPaths[i];
                    string relative = GetRelativeDisplayPath(sourceFolder, path);
                    bool current = fbxSelection[path];
                    bool updated = EditorGUILayout.ToggleLeft(relative, current);
                    if (updated != current)
                    {
                        fbxSelection[path] = updated;
                    }
                }
                EditorGUILayout.EndScrollView();

                EditorGUILayout.LabelField(scannedPngPaths.Count + " texture file(s) found and will all be copied over.",
                    EditorStyles.miniLabel);
            }
        }

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("2. Import Into", EditorStyles.boldLabel);
        importFolder = EditorGUILayout.TextField("Assets Folder", importFolder);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("3. Naming Convention", EditorStyles.boldLabel);
        convention = (NamingConvention)EditorGUILayout.EnumPopup("Convention", convention);
        EditorGUILayout.HelpBox(
            "Auto Detect scans the found textures for either an \"Oak_*\" file " +
            "(Oak Seasonal) or a \"bark_albedo.png\"/\"branch_NN_card.png\" file " +
            "(Procedural Conifer) and picks accordingly.", MessageType.None);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("4. Season (Oak Seasonal only)", EditorStyles.boldLabel);
        season = (Season)EditorGUILayout.EnumPopup("Season", season);

        EditorGUILayout.Space();
        EditorGUILayout.LabelField("5. Terrain", EditorStyles.boldLabel);
        registerOnTerrain = EditorGUILayout.Toggle("Register On Terrain", registerOnTerrain);
        GUI.enabled = registerOnTerrain;
        targetTerrain = (Terrain)EditorGUILayout.ObjectField(
            "Target Terrain", targetTerrain, typeof(Terrain), true);
        GUI.enabled = true;

        EditorGUILayout.Space();
        EditorGUILayout.Space();

        int selectedCount = CountSelectedFbx();
        GUI.enabled = hasScanned && selectedCount > 0 && (!registerOnTerrain || targetTerrain != null);
        string buttonLabel = selectedCount > 0
            ? "Generate & Setup (" + selectedCount + " selected)"
            : "Generate & Setup";
        if (GUILayout.Button(buttonLabel, GUILayout.Height(36f)))
        {
            RunSetup();
        }
        GUI.enabled = true;

        if (registerOnTerrain && targetTerrain == null)
        {
            EditorGUILayout.HelpBox("Assign a Terrain, or uncheck Register On Terrain.", MessageType.Warning);
        }
    }

    private int CountSelectedFbx()
    {
        int count = 0;
        foreach (KeyValuePair<string, bool> kv in fbxSelection)
        {
            if (kv.Value)
            {
                count++;
            }
        }
        return count;
    }

    private void SetAllFbxSelection(bool value)
    {
        List<string> keys = new List<string>(fbxSelection.Keys);
        for (int i = 0; i < keys.Count; i++)
        {
            fbxSelection[keys[i]] = value;
        }
    }

    private string GetRelativeDisplayPath(string root, string fullPath)
    {
        string normalizedRoot = root.Replace("\\", "/");
        string normalizedFull = fullPath.Replace("\\", "/");
        if (normalizedFull.StartsWith(normalizedRoot))
        {
            string rel = normalizedFull.Substring(normalizedRoot.Length);
            if (rel.StartsWith("/"))
            {
                rel = rel.Substring(1);
            }
            return rel;
        }
        return normalizedFull;
    }

    private void ScanFolder()
    {
        scannedFbxPaths.Clear();
        fbxSelection.Clear();
        scannedPngPaths.Clear();
        hasScanned = false;

        if (string.IsNullOrEmpty(sourceFolder) || !Directory.Exists(sourceFolder))
        {
            EditorUtility.DisplayDialog("Invalid Folder", "Pick a valid source folder first.", "OK");
            return;
        }

        string[] fbxFiles = Directory.GetFiles(sourceFolder, "*.fbx", SearchOption.AllDirectories);
        for (int i = 0; i < fbxFiles.Length; i++)
        {
            scannedFbxPaths.Add(fbxFiles[i]);
            fbxSelection.Add(fbxFiles[i], true);   // default: everything selected
        }

        string[] pngFiles = Directory.GetFiles(sourceFolder, "*.png", SearchOption.AllDirectories);
        for (int i = 0; i < pngFiles.Length; i++)
        {
            string fileName = Path.GetFileName(pngFiles[i]);
            if (!scannedPngPaths.ContainsKey(fileName))
            {
                scannedPngPaths.Add(fileName, pngFiles[i]);
            }
            else
            {
                Debug.LogWarning("Tree Setup Wizard: duplicate texture filename '" + fileName +
                    "' found in more than one subfolder -- using " + scannedPngPaths[fileName] +
                    " and ignoring " + pngFiles[i] + ".");
            }
        }

        lastScannedFolder = sourceFolder;
        hasScanned = true;

        if (scannedFbxPaths.Count == 0)
        {
            EditorUtility.DisplayDialog("No FBX Found", "No .fbx files were found under:\n" + sourceFolder, "OK");
        }
    }

    private void RunSetup()
    {
        if (!hasScanned)
        {
            EditorUtility.DisplayDialog("Scan First", "Click \"Scan Folder\" before generating.", "OK");
            return;
        }
        if (sourceFolder != lastScannedFolder)
        {
            EditorUtility.DisplayDialog("Rescan Needed",
                "The source folder changed since your last scan. Click \"Scan Folder\" again.", "OK");
            return;
        }

        List<string> fbxToProcess = new List<string>();
        for (int i = 0; i < scannedFbxPaths.Count; i++)
        {
            if (fbxSelection[scannedFbxPaths[i]])
            {
                fbxToProcess.Add(scannedFbxPaths[i]);
            }
        }
        if (fbxToProcess.Count == 0)
        {
            EditorUtility.DisplayDialog("Nothing Selected", "Select at least one FBX file to process.", "OK");
            return;
        }

        NamingConvention resolvedConvention = convention;
        if (resolvedConvention == NamingConvention.AutoDetect)
        {
            resolvedConvention = DetectConvention(scannedPngPaths);
        }
        Debug.Log("Tree Setup Wizard: using naming convention = " + resolvedConvention.ToString());

        if (resolvedConvention == NamingConvention.OakSeasonal)
        {
            Shader knotShader = Shader.Find("Custom/OakBarkKnotVariant");
            Shader windShader = Shader.Find("Custom/OakFoliageWind");
            if (knotShader == null || windShader == null)
            {
                Debug.LogWarning("Tree Setup Wizard: Custom/OakBarkKnotVariant and/or " +
                    "Custom/OakFoliageWind not found -- bark/foliage materials will fall " +
                    "back to the Standard shader instead.");
            }
        }

        EnsureFolder(importFolder);

        // --- copy only the selected fbx files into the project ---
        List<string> importedFbxAssetPaths = new List<string>();
        for (int i = 0; i < fbxToProcess.Count; i++)
        {
            string fileName = Path.GetFileName(fbxToProcess[i]);
            string destPath = importFolder + "/" + fileName;
            File.Copy(fbxToProcess[i], GetSystemPath(destPath), true);
            importedFbxAssetPaths.Add(destPath);
        }

        // --- copy every png found in the recursive scan into the project ---
        Dictionary<string, string> pngAssetPaths = new Dictionary<string, string>();
        foreach (KeyValuePair<string, string> kv in scannedPngPaths)
        {
            string destPath = importFolder + "/" + kv.Key;
            File.Copy(kv.Value, GetSystemPath(destPath), true);
            pngAssetPaths.Add(kv.Key, destPath);
        }

        AssetDatabase.Refresh();

        // --- fix texture import settings by filename convention ---
        foreach (KeyValuePair<string, string> kv in pngAssetPaths)
        {
            string lower = kv.Key.ToLower();
            bool isNormal = lower.Contains("normal");
            bool isLinear = isNormal || lower.Contains("rough") || lower.Contains("mask");
            ConfigureTexture(kv.Value, isNormal, isLinear);
        }
        AssetDatabase.Refresh();

        Dictionary<string, Material> materialCache = new Dictionary<string, Material>();
        List<GameObject> createdPrefabs = new List<GameObject>();
        int slotsSkipped = 0;

        for (int i = 0; i < importedFbxAssetPaths.Count; i++)
        {
            string fbxPath = importedFbxAssetPaths[i];
            GameObject fbxAsset = AssetDatabase.LoadAssetAtPath<GameObject>(fbxPath);
            if (fbxAsset == null)
            {
                Debug.LogWarning("Tree Setup Wizard: couldn't load " + fbxPath + " as a GameObject -- skipping.");
                continue;
            }

            GameObject instance = (GameObject)PrefabUtility.InstantiatePrefab(fbxAsset);
            if (instance == null)
            {
                Debug.LogWarning("Tree Setup Wizard: couldn't instantiate " + fbxPath + " -- skipping.");
                continue;
            }

            List<Renderer> renderers = FindRenderersToProcess(instance);
            for (int r = 0; r < renderers.Count; r++)
            {
                slotsSkipped += ProcessRenderer(renderers[r], resolvedConvention, pngAssetPaths, materialCache);
            }

            string baseName = Path.GetFileNameWithoutExtension(fbxPath);
            string prefabPath = importFolder + "/" + baseName + "_Prefab.prefab";
#if UNITY_2018_3_OR_NEWER
            GameObject prefabAsset = PrefabUtility.SaveAsPrefabAsset(instance, prefabPath);
#else
            GameObject prefabAsset = PrefabUtility.CreatePrefab(prefabPath, instance, ReplacePrefabOptions.ReplaceNameBased);
#endif
            if (prefabAsset != null)
            {
                createdPrefabs.Add(prefabAsset);
            }
            else
            {
                Debug.LogWarning("Tree Setup Wizard: couldn't save a prefab for " + fbxPath + ".");
            }
        }

        AssetDatabase.SaveAssets();

        bool didRegister = false;
        if (registerOnTerrain && targetTerrain != null && createdPrefabs.Count > 0)
        {
            RegisterPrototypes(targetTerrain, createdPrefabs);
            didRegister = true;
        }

        string summary = "Set up " + createdPrefabs.Count + " tree prefab(s) in " + importFolder + ".";
        if (slotsSkipped > 0)
        {
            summary += "\n" + slotsSkipped + " material slot(s) had no matching texture and were left as-is " +
                "(see Console for which ones).";
        }
        if (didRegister)
        {
            summary += "\nRegistered on terrain: " + targetTerrain.name;
        }

        Debug.Log("Tree Setup Wizard: " + summary.Replace("\n", " "));
        EditorUtility.DisplayDialog("Done", summary, "Great");
    }

    // =====================================================================
    // Convention detection
    // =====================================================================

    private NamingConvention DetectConvention(Dictionary<string, string> pngFiles)
    {
        foreach (string fileName in pngFiles.Keys)
        {
            if (fileName.StartsWith("Oak_"))
            {
                return NamingConvention.OakSeasonal;
            }
        }
        foreach (string fileName in pngFiles.Keys)
        {
            string lower = fileName.ToLower();
            if (lower == "bark_albedo.png" || Regex.IsMatch(lower, @"^branch_\d+_card"))
            {
                return NamingConvention.ProceduralConifer;
            }
        }
        // Nothing recognized -- default to this project's convention.
        return NamingConvention.ProceduralConifer;
    }

    // =====================================================================
    // Renderer discovery -- handles both the Oak-style named-LOD-children
    // hierarchy and this project's single-renderer-with-many-slots export
    // =====================================================================

    private List<Renderer> FindRenderersToProcess(GameObject root)
    {
        List<Renderer> result = new List<Renderer>();

        Transform[] allChildren = root.GetComponentsInChildren<Transform>(true);
        bool foundLodChild = false;
        for (int i = 0; i < allChildren.Length; i++)
        {
            if (allChildren[i].name.ToLower().Contains("_lod"))
            {
                Renderer rend = allChildren[i].GetComponent<Renderer>();
                if (rend != null)
                {
                    result.Add(rend);
                    foundLodChild = true;
                }
            }
        }
        if (foundLodChild)
        {
            return result;
        }

        // No Oak-style LOD children found -- fall back to every renderer in
        // the hierarchy, which covers this project's single joined mesh.
        Renderer[] all = root.GetComponentsInChildren<Renderer>(true);
        for (int i = 0; i < all.Length; i++)
        {
            result.Add(all[i]);
        }
        return result;
    }

    // Returns the number of material slots that had no texture match.
    private int ProcessRenderer(Renderer renderer, NamingConvention conv,
        Dictionary<string, string> pngAssetPaths, Dictionary<string, Material> cache)
    {
        Material[] slots = renderer.sharedMaterials;
        Material[] replaced = new Material[slots.Length];
        int skipped = 0;

        for (int i = 0; i < slots.Length; i++)
        {
            string slotName = slots[i] != null ? slots[i].name : "";
            Material built = (conv == NamingConvention.OakSeasonal)
                ? BuildOakMaterial(slotName, pngAssetPaths, cache)
                : BuildConiferMaterial(slotName, pngAssetPaths, cache);

            if (built != null)
            {
                replaced[i] = built;
            }
            else
            {
                replaced[i] = slots[i];
                skipped++;
                Debug.LogWarning("Tree Setup Wizard: no texture match for material slot '" + slotName +
                    "' on " + renderer.gameObject.name + " -- leaving Unity's auto-imported material.");
            }
        }

        renderer.sharedMaterials = replaced;
        return skipped;
    }

    // =====================================================================
    // Procedural Conifer convention (this project)
    // =====================================================================

    private Material BuildConiferMaterial(string slotName, Dictionary<string, string> pngAssetPaths,
        Dictionary<string, Material> cache)
    {
        if (cache.ContainsKey(slotName))
        {
            return cache[slotName];
        }

        Material mat = null;

        if (slotName.ToLower().StartsWith("barkmaterial"))
        {
            string albedoPath;
            if (!pngAssetPaths.TryGetValue("bark_albedo.png", out albedoPath))
            {
                return null;
            }

            mat = new Material(Shader.Find("Standard"));
            mat.name = slotName;
            mat.SetTexture("_MainTex", AssetDatabase.LoadAssetAtPath<Texture2D>(albedoPath));

            string normalPath;
            if (pngAssetPaths.TryGetValue("bark_normal.png", out normalPath))
            {
                mat.SetTexture("_BumpMap", AssetDatabase.LoadAssetAtPath<Texture2D>(normalPath));
                mat.EnableKeyword("_NORMALMAP");
            }

            string speciesKey = ExtractSpeciesKey(slotName);
            if (!string.IsNullOrEmpty(speciesKey) && SpeciesBarkTints.ContainsKey(speciesKey))
            {
                mat.color = SpeciesBarkTints[speciesKey];
            }
        }
        else
        {
            Match m = Regex.Match(slotName, @"(\d+)");
            if (!m.Success)
            {
                return null;
            }
            string index = m.Groups[1].Value;
            string albedoFile = "branch_" + index + "_card.png";
            string normalFile = "branch_" + index + "_card_normal.png";

            string albedoPath;
            if (!pngAssetPaths.TryGetValue(albedoFile, out albedoPath))
            {
                return null;
            }

            mat = new Material(Shader.Find("Standard"));
            mat.name = slotName;
            SetStandardShaderCutout(mat);
            mat.SetTexture("_MainTex", AssetDatabase.LoadAssetAtPath<Texture2D>(albedoPath));

            string normalPath;
            if (pngAssetPaths.TryGetValue(normalFile, out normalPath))
            {
                mat.SetTexture("_BumpMap", AssetDatabase.LoadAssetAtPath<Texture2D>(normalPath));
                mat.EnableKeyword("_NORMALMAP");
            }
        }

        string assetPath = importFolder + "/" + SanitizeFileName(slotName) + ".mat";
        AssetDatabase.CreateAsset(mat, AssetDatabase.GenerateUniqueAssetPath(assetPath));
        cache.Add(slotName, mat);
        return mat;
    }

    private string ExtractSpeciesKey(string slotName)
    {
        // "BarkMaterial_pine_red" -> "pine_red"
        string prefix = "BarkMaterial_";
        int idx = slotName.IndexOf(prefix);
        if (idx < 0)
        {
            return null;
        }
        return slotName.Substring(idx + prefix.Length);
    }

    // =====================================================================
    // Oak Seasonal convention (legacy pipeline, kept working as before)
    // =====================================================================

    private Material BuildOakMaterial(string slotName, Dictionary<string, string> pngAssetPaths,
        Dictionary<string, Material> cache)
    {
        string seasonLabel = season.ToString();
        string cacheKey = slotName + "_" + seasonLabel;
        if (cache.ContainsKey(cacheKey))
        {
            return cache[cacheKey];
        }

        string lower = slotName.ToLower();
        Material mat = null;

        if (lower.Contains("bark") || lower.Contains("knot"))
        {
            Shader knotShader = Shader.Find("Custom/OakBarkKnotVariant");
            mat = new Material(knotShader != null ? knotShader : Shader.Find("Standard"));
            mat.name = slotName;
            AssignIfFound(mat, "_MainTex", pngAssetPaths, "Oak_Bark_Albedo_" + seasonLabel + ".png");
            AssignIfFound(mat, "_BumpMap", pngAssetPaths, "Oak_Bark_Normal_" + seasonLabel + ".png");
            AssignIfFound(mat, "_RoughnessMap", pngAssetPaths, "Oak_Bark_Roughness_" + seasonLabel + ".png");
            AssignIfFound(mat, "_KnotMask", pngAssetPaths, "Oak_Bark_KnotMask.png");
        }
        else if (lower.Contains("billboard"))
        {
            mat = new Material(Shader.Find("Standard"));
            mat.name = slotName;
            SetStandardShaderCutout(mat);
            AssignIfFound(mat, "_MainTex", pngAssetPaths, "Oak_Billboard.png");
        }
        else if (lower.Contains("wind") || lower.Contains("foliage"))
        {
            Shader windShader = Shader.Find("Custom/OakFoliageWind");
            mat = new Material(windShader != null ? windShader : Shader.Find("Standard"));
            mat.name = slotName;
            AssignIfFound(mat, "_MainTex", pngAssetPaths, "Oak_Foliage_Albedo_" + seasonLabel + ".png");
            AssignIfFound(mat, "_BumpMap", pngAssetPaths, "Oak_Foliage_Normal_" + seasonLabel + ".png");
        }
        else
        {
            return null;
        }

        string assetPath = importFolder + "/" + SanitizeFileName(cacheKey) + ".mat";
        AssetDatabase.CreateAsset(mat, AssetDatabase.GenerateUniqueAssetPath(assetPath));
        cache.Add(cacheKey, mat);
        return mat;
    }

    private void AssignIfFound(Material mat, string property, Dictionary<string, string> pngAssetPaths, string fileName)
    {
        string path;
        if (pngAssetPaths.TryGetValue(fileName, out path))
        {
            mat.SetTexture(property, AssetDatabase.LoadAssetAtPath<Texture2D>(path));
        }
    }

    // =====================================================================
    // Shared helpers
    // =====================================================================

    private void SetStandardShaderCutout(Material mat)
    {
        // Mirrors what Unity's own Standard Shader GUI does when you switch
        // Rendering Mode to Cutout in the Inspector -- setting the texture
        // alone isn't enough, these keywords/blend states are required too.
        mat.SetFloat("_Mode", 1f); // 1 = Cutout
        mat.SetFloat("_Cutoff", 0.5f);
        mat.SetInt("_SrcBlend", (int)UnityEngine.Rendering.BlendMode.One);
        mat.SetInt("_DstBlend", (int)UnityEngine.Rendering.BlendMode.Zero);
        mat.SetInt("_ZWrite", 1);
        mat.DisableKeyword("_ALPHABLEND_ON");
        mat.EnableKeyword("_ALPHATEST_ON");
        mat.DisableKeyword("_ALPHAPREMULTIPLY_ON");
        mat.renderQueue = (int)UnityEngine.Rendering.RenderQueue.AlphaTest;
    }

    private void ConfigureTexture(string assetPath, bool isNormalMap, bool isLinearData)
    {
        TextureImporter importer = AssetImporter.GetAtPath(assetPath) as TextureImporter;
        if (importer == null)
        {
            return;
        }
        if (isNormalMap)
        {
            importer.textureType = TextureImporterType.NormalMap;
        }
        else
        {
            importer.textureType = TextureImporterType.Default;
            importer.sRGBTexture = !isLinearData;
        }
        importer.SaveAndReimport();
    }

    private void RegisterPrototypes(Terrain terrain, List<GameObject> prefabs)
    {
        TerrainData terrainData = terrain.terrainData;
        List<TreePrototype> prototypes = new List<TreePrototype>(terrainData.treePrototypes);
        for (int i = 0; i < prefabs.Count; i++)
        {
            TreePrototype proto = new TreePrototype();
            proto.prefab = prefabs[i];
            proto.bendFactor = 0f; // wind sway, if any, is handled by a custom shader, not Unity's built-in tree bending
            prototypes.Add(proto);
        }
        terrainData.treePrototypes = prototypes.ToArray();
        terrainData.RefreshPrototypes();
        EditorUtility.SetDirty(terrainData);
        AssetDatabase.SaveAssets();
    }

    private void EnsureFolder(string assetFolderPath)
    {
        if (AssetDatabase.IsValidFolder(assetFolderPath))
        {
            return;
        }
        string parent = Path.GetDirectoryName(assetFolderPath);
        if (string.IsNullOrEmpty(parent))
        {
            parent = "Assets";
        }
        parent = parent.Replace("\\", "/");
        string folderName = Path.GetFileName(assetFolderPath);

        if (!AssetDatabase.IsValidFolder(parent))
        {
            EnsureFolder(parent);
        }
        AssetDatabase.CreateFolder(parent, folderName);
    }

    private string GetSystemPath(string assetPath)
    {
        string projectRoot = Application.dataPath.Substring(0, Application.dataPath.Length - "Assets".Length);
        return Path.Combine(projectRoot, assetPath);
    }

    private string SanitizeFileName(string name)
    {
        char[] invalid = Path.GetInvalidFileNameChars();
        for (int i = 0; i < invalid.Length; i++)
        {
            name = name.Replace(invalid[i].ToString(), "_");
        }
        return name;
    }
}
