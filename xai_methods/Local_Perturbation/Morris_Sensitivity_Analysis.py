import torch
import torch.nn.functional as F
import cv2
import numpy as np
import matplotlib.pyplot as plt
import timeit
from utils.data_utils import process_data_tuple
from models.label_utils import get_label_mapping

def generate_attribution(image, predicted_class, model, patch_size=16, num_samples=10, delta=0.05):
    """
    Generate Morris Sensitivity Analysis (MSA) attribution for the given image and class.
    This method perturbs small patches of the input by adding a small delta and computes the change 
    in the predicted probability to estimate the sensitivity of each patch.
    
    Parameters:
    - image: Tensor representing the input image (shape: [1, 3, H, W]).
    - predicted_class: Predicted class label for which we compute the attribution.
    - model: Pretrained model.
    - patch_size: Size of the patch to perturb (default: 16).
    - num_samples: Number of perturbations to average per patch.
    - delta: The small additive perturbation value.
    
    Returns:
    - heatmap: A 2D tensor (upsampled to input size) representing the sensitivity of each region.
    """
    model.eval()
    device = image.device

    # Get the original predicted probability for the target class
    with torch.no_grad():
        output = model(image)
        orig_prob = F.softmax(output, dim=1)[0, predicted_class.item()]
    
    # Get image spatial dimensions
    _, _, H, W = image.shape
    num_patches_h = H // patch_size
    num_patches_w = W // patch_size

    # Prepare an empty heatmap (one value per patch)
    heatmap = torch.zeros((num_patches_h, num_patches_w), device=device)

    # Iterate over patches
    for i in range(num_patches_h):
        for j in range(num_patches_w):
            effects = []
            for s in range(num_samples):
                # Clone the original image to create a perturbed version
                perturbed_image = image.clone()
                
                # Add a small perturbation (delta) to the current patch region
                perturbed_image[:, :, i*patch_size:(i+1)*patch_size, j*patch_size:(j+1)*patch_size] += delta
                
                # Compute the model's output for the perturbed image
                with torch.no_grad():
                    out = model(perturbed_image)
                    new_prob = F.softmax(out, dim=1)[0, predicted_class.item()]
                
                # Compute the elementary effect for this patch perturbation
                effect = (new_prob - orig_prob) / delta
                effects.append(effect.item())
            
            # Average the effect over the number of samples for this patch
            heatmap[i, j] = sum(effects) / len(effects)
    
    # Use the absolute value of the sensitivity and normalize for visualization
    heatmap = torch.abs(heatmap)
    eps = 1e-8
    heat_min = heatmap.min()
    heat_max = heatmap.max()
    if (heat_max - heat_min) > eps:
        heatmap = (heatmap - heat_min) / (heat_max - heat_min)
    else:
        heatmap.zero_()
    
    # Upsample the heatmap to match the input image size
    heatmap = heatmap.unsqueeze(0).unsqueeze(0)  # shape: [1, 1, num_patches_h, num_patches_w]
    heatmap = F.interpolate(heatmap, size=(H, W), mode='bilinear', align_corners=False)
    heatmap = heatmap.squeeze()  # shape: [H, W]
    
    return heatmap


def warm_up(model):
    """
    Run a warm-up pass to ensure memory and computation stability.
    
    Parameters:
    - model: Pretrained model.
    """
    dummy_image = torch.randn(1, 3, 224, 224, device=next(model.parameters()).device)
    with torch.no_grad():
        output = model(dummy_image)
        _, predicted_class = torch.max(output, 1)
    _ = generate_attribution(dummy_image, predicted_class, model)


def visualize_attribution(image, attribution, label, label_names, model, model_name, save_path=None):
    """
    Visualize the Morris Sensitivity Analysis heatmap overlaid on the original image.
    
    Parameters:
    - image: Original input image tensor.
    - attribution: Sensitivity heatmap (2D tensor) generated by MSA.
    - label: True label (or None for URL data).
    - label_names: List of class names for the dataset.
    - model: Pretrained model.
    - model_name: Name of the model.
    - save_path: Optional file path to save the visualization.
    """
    with torch.no_grad():
        output = model(image)
        _, predicted_class = torch.max(output, 1)
    
    # Get label mappings for predicted and true labels
    predicted_label, true_label = get_label_mapping(
        model_name=model_name,
        predicted_class=predicted_class,
        label=label,
        label_names=label_names,
    )
    
    print(f"Model: {model_name}, Predicted Class: {predicted_label}, True Class: {true_label}")
    
    # Process the heatmap for visualization
    heatmap_np = attribution.cpu().numpy()
    heatmap_np = (heatmap_np * 255).astype(np.uint8)
    heatmap_colored = cv2.applyColorMap(heatmap_np, cv2.COLORMAP_JET)
    
    # Convert the original image to a NumPy array (reverse normalization if needed)
    img_np = image.squeeze().permute(1, 2, 0).cpu().numpy()
    img_np = np.clip((img_np * [0.229, 0.224, 0.225] + [0.485, 0.456, 0.406]) * 255, 0, 255).astype(np.uint8)
    
    # Overlay the heatmap on the original image
    overlayed_img = cv2.addWeighted(img_np, 0.6, heatmap_colored, 0.4, 0)
    
    # Plot the results
    fig, axes = plt.subplots(1, 2, figsize=(10, 5))
    axes[0].imshow(img_np)
    title_input = f'Input' if true_label == "Unknown" else f'True: {true_label}'
    axes[0].set_title(title_input)
    axes[0].axis('off')
    
    axes[1].imshow(overlayed_img)
    axes[1].set_title(f'Predicted: {predicted_label}')
    axes[1].axis('off')
    
    fig.tight_layout()
    
    if save_path:
        fig.savefig(save_path, bbox_inches="tight", pad_inches=0.1)
        visualize_attribution.save_count += 1
        print(f"Saved visualization at {save_path}")
    
    plt.show()
    plt.close(fig)


# Initialize the save counter as an attribute of the function
visualize_attribution.save_count = 0


def measure_avg_time_across_images(data_source, model, generate_attribution):
    """
    Measure the average time taken to generate Morris Sensitivity Analysis attributions across images.
    
    Parameters:
    - data_source: DataLoader or list of image tuples.
    - model: Pretrained model.
    - generate_attribution: Function to generate attributions.
    
    Returns:
    - avg_time_taken: Average time taken.
    - times: List of times for each image.
    """
    times = []
    for data_item in data_source:
        image, label = process_data_tuple(data_item, model)
        with torch.no_grad():
            output = model(image)
            _, predicted_class = torch.max(output, 1)
        
        start_time = timeit.default_timer()
        _ = generate_attribution(image, predicted_class, model)
        end_time = timeit.default_timer()
        
        times.append(end_time - start_time)
        torch.cuda.empty_cache()
    
    avg_time_taken = sum(times) / len(times)
    return avg_time_taken, times
